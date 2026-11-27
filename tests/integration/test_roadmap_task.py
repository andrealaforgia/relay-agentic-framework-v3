"""The Analyst's work carried out by a worker: a feature's first roadmap, a change to it,
the correction of a rejected story. The reply is a proposal put to the Product Owner, or
one question in its place. The provider is scripted; what is real is everything around it."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.authoring import RoadmapTask
from relay.application.invocations import Invocations
from relay.application.tasks import Profile, Tasks, Workspaces
from relay.application.worker import Worker
from relay.application.workers import WorkerSession
from relay.domain.story import HumanAction
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.test_review_task import Reviewer
from tests.integration.test_worker import RUNTIME, SeenToHold
from tests.integration.trunk_world import PROJECT

S1 = {
    "change": "new",
    "story_id": "S1",
    "title": "Standard rate",
    "outcome": "a gross price is shown",
    "criteria": [{"id": "AC1", "text": "100 net is shown as 120"}],
}
S2 = {**S1, "story_id": "S2", "title": "Reduced rate"}


def proposing(*stories: dict[str, Any], assuming: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "outcome": "delivered",
        "assumptions": list(assuming),
        "delivered": {"stories": list(stories)},
    }


ASKING = {
    "outcome": "question",
    "assumptions": [],
    "question": {
        "text": "Which rate applies to books?",
        "why": "it decides whether there is a second story",
        "options": ["standard", "zero"],
    },
}


class Planning:
    def __init__(self, world: DecisionWorld, tmp_path: Path) -> None:
        self.world = world
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "alf-user-story-writer.md").write_text(
            "---\nname: alf-user-story-writer\n---\nWrite stories.\n"
        )
        self.invocations = Invocations(world.database)
        self.task = RoadmapTask(
            world.database,
            self.invocations,
            Workspaces(tmp_path.resolve() / "workspaces"),
            world.mirrors,
            [agents],
            Profile("claude-sonnet-5-5"),
        )
        self.tasks = {"plan": self.task, "revise": self.task, "correct": self.task}
        Tasks(self.tasks).attach(self.invocations)
        self.sessions: list[WorkerSession] = []

    async def worker(self, provider: Any) -> Worker:
        session = await WorkerSession.start(self.world.database, "analyst")
        self.sessions.append(session)
        return Worker(
            session,
            self.world.claims,
            self.invocations,
            provider,
            self.tasks,
            SeenToHold(),
            lambda: RUNTIME,
        )

    async def owed(self, kind: str = "plan") -> dict[str, Any]:
        """The latest work of this kind, whatever has become of it."""
        async with self.world.database.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE kind = %s AND project_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (kind, PROJECT),
                )
            ).fetchone()
        assert row is not None
        return row

    async def gate(self) -> uuid.UUID:
        [item] = await self.world.stories.inbox(PROJECT)
        return item.gate_id

    async def states(self, kind: str = "plan") -> list[object]:
        return await self.world.column(
            f"SELECT state FROM assignments WHERE kind = '{kind}' ORDER BY created_at"
        )


@pytest.fixture
async def planning(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Planning]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    planning = Planning(world, tmp_path)
    yield planning
    await planning.invocations.close()
    for session in planning.sessions:
        await session.stop()


async def test_an_opened_feature_owes_its_plan_and_the_reply_is_put_to_the_product_owner(
    planning: Planning,
) -> None:
    analyst = Reviewer(proposing(S1, S2))
    worker = await planning.worker(analyst)

    assert await worker.carry_out(await planning.owed()) == "applied"

    [request] = analyst.asked
    assert "prices need VAT" in request.briefing
    assert analyst.found[0]["README"] == "a VAT calculator\n"  # the trunk, to read
    [item] = await planning.world.stories.inbox(PROJECT)
    assert [story["story_id"] for story in item.manifest["stories"]] == ["S1", "S2"]
    # Proposing is not the end of it: the plan is owed until a roadmap is in force.
    assert await planning.states() == ["granted"]
    assert list(planning.task._workspaces._root.iterdir()) == []

    assert (await planning.world.decide(item.gate_id)).disposition == "effective"

    assert await planning.states() == ["done"]
    assert (await planning.world.stories.roadmap_of(PROJECT, "F1")).stories == ("S1", "S2")


async def test_a_proposal_sent_back_is_owed_again_and_briefed_with_why(
    planning: Planning,
) -> None:
    analyst = Reviewer(proposing(S1, S2), proposing(S1))
    worker = await planning.worker(analyst)
    first = await planning.owed()
    assert await worker.carry_out(first) == "applied"

    await planning.world.decide(
        await planning.gate(), "request_changes", reason="one story is enough"
    )

    assert await planning.states() == ["done", "wanted"]
    successor = await planning.owed()
    assert successor["succeeds"] == first["assignment_id"]
    # The old announcement, delivered again, is of an occasion that is over.
    assert await worker.carry_out(first) == "refused: this work is no longer wanted"
    assert len(analyst.asked) == 1

    assert await worker.carry_out(successor) == "applied"

    assert "your proposal was sent back: one story is enough" in analyst.asked[1].briefing
    [item] = await planning.world.stories.inbox(PROJECT)
    assert [story["story_id"] for story in item.manifest["stories"]] == ["S1"]


async def test_a_reply_that_asks_ends_its_occasion_and_the_answer_owes_the_work_again(
    planning: Planning,
) -> None:
    """S3-19.1: one further call, admitted on the basis the answer made."""
    analyst = Reviewer(ASKING, proposing(S1))
    worker = await planning.worker(analyst)
    first = await planning.owed()

    assert await worker.carry_out(first) == "applied"

    assert await planning.states() == ["asked"]
    [question] = await planning.world.decisions.open_questions(PROJECT)
    assert question.text == "Which rate applies to books?"
    assert (await worker.carry_out(first)).startswith("refused: the feature is waiting on")

    await planning.world.answer(question.question_id, "zero")

    assert await planning.states() == ["asked", "wanted"]
    # Delivered again beside its successor, the old command calls nothing.
    assert await worker.carry_out(first) == "refused: this work is no longer wanted"
    assert await worker.carry_out(await planning.owed()) == "applied"
    assert len(analyst.asked) == 2
    assert "Which rate applies to books? The Product Owner answered: zero" in (
        analyst.asked[1].briefing
    )
    assert await planning.world.column(
        "SELECT (admitted_on->>'basis')::integer FROM invocations ORDER BY attempt, invocation_id"
    ) == [0, 1]


async def test_what_the_model_assumed_is_on_record_beside_what_it_delivered(
    planning: Planning,
) -> None:
    worker = await planning.worker(Reviewer(proposing(S1, assuming=("prices are in euros",))))

    assert await worker.carry_out(await planning.owed()) == "applied"

    assert await planning.world.column("SELECT source, text FROM decisions") == ["model"]
    assert await planning.world.column("SELECT text FROM decisions") == ["prices are in euros"]
    assert len(await planning.world.stories.inbox(PROJECT)) == 1


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (proposing({**S1, "story_id": "not a name"}), "not in the form asked for"),
        (proposing({"change": "new", "story_id": "S1"}), "not in the form asked for"),
        (proposing(), "at least one story"),
        ({**ASKING, "delivered": {"stories": [S1]}}, "delivers nothing"),
        ({"outcome": "delivered", "assumptions": []}, "what was asked for"),
        (
            proposing({**S1, "criteria": [{"id": "AC1", "text": "a"}, {"id": "AC1", "text": "b"}]}),
            "names a criterion twice",
        ),
    ],
)
async def test_a_reply_that_is_no_roadmap_proposes_nothing_and_is_asked_for_again(
    planning: Planning, reply: dict[str, Any], why: str
) -> None:
    analyst = Reviewer(reply, proposing(S1))
    worker = await planning.worker(analyst)

    assert await worker.carry_out(await planning.owed()) == "applied"

    assert await planning.world.column("SELECT state FROM invocations ORDER BY attempt") == [
        "rejected",
        "applied",
    ]
    assert why in analyst.asked[1].briefing
    assert await planning.world.column("SELECT revision FROM roadmaps") == [1]


async def test_a_proposal_the_service_refuses_is_a_reply_that_gave_nothing(
    planning: Planning,
) -> None:
    """A story that is kept and was never there: refused under the lock, with the mark."""
    refused = proposing({"change": "kept", "story_id": "S9"}, assuming=("S9 exists",))
    analyst = Reviewer(refused, proposing(S1))
    worker = await planning.worker(analyst)

    assert await worker.carry_out(await planning.owed()) == "applied"

    assert "S9 is not a story of the roadmap being revised" in analyst.asked[1].briefing
    assert await planning.world.column("SELECT revision FROM roadmaps") == [1]
    # 3B-S3-1.12: what a refused reply assumed is nobody's assumption.
    assert await planning.world.column("SELECT text FROM decisions") == []


async def test_what_a_reply_that_asks_assumed_is_on_record_with_its_question(
    planning: Planning,
) -> None:
    worker = await planning.worker(Reviewer({**ASKING, "assumptions": ["books are goods"]}))

    assert await worker.carry_out(await planning.owed()) == "applied"

    assert await planning.world.column("SELECT text FROM decisions") == ["books are goods"]
    assert len(await planning.world.decisions.open_questions(PROJECT)) == 1


async def test_a_proposal_written_while_the_roadmap_moved_is_overtaken_and_blamed_for_nothing(
    planning: Planning,
) -> None:
    class WhileAnotherProposes(Reviewer):
        async def invoke(self, request: Any, **named: Any) -> Any:
            if not self.asked:
                await planning.world.revise(STANDARD_RATE)
            return await super().invoke(request, **named)

    analyst = WhileAnotherProposes(proposing(S2))
    worker = await planning.worker(analyst)

    assert await worker.carry_out(await planning.owed()) == "overtaken"

    assert await planning.world.column("SELECT revision FROM roadmaps") == [1]
    assert await planning.world.column("SELECT reason FROM invocations") == [
        "the roadmap moved on while this proposal was being written"
    ]
    # Still owed, and not to be proposed while the other waits to be decided.
    assert await planning.states() == ["wanted"]
    assert (await worker.carry_out(await planning.owed())).startswith(
        "refused: a proposal for this roadmap is waiting to be decided"
    )
    assert len(analyst.asked) == 1


async def test_a_rejected_story_is_corrected_by_a_worker_and_the_correction_is_done_on_approval(
    planning: Planning,
) -> None:
    world = planning.world
    await world.decide(await world.revise(STANDARD_RATE))
    world.requests += 1
    await world.stories.reject(
        PROJECT,
        HumanAction(
            PRODUCT_OWNER,
            f"reject-{world.requests}",
            "S1",
            "reject",
            {"reason": "VAT is out of scope", "revision": "1", "basis": "0"},
        ),
    )
    analyst = Reviewer(proposing({"change": "kept", "story_id": "S1"}, {**S2, "corrects": "S1"}))
    worker = await planning.worker(analyst)

    assert await worker.carry_out(await planning.owed("correct")) == "applied"

    assert "Story S1 was rejected" in analyst.asked[0].briefing
    # 3B-S3-1.13: rejected while it was being worked on, and told why all the same.
    assert "Rejected by the Product Owner: VAT is out of scope" in analyst.asked[0].briefing
    assert await planning.states("correct") == ["granted"]
    await world.decide(await planning.gate())
    assert await planning.states("correct") == ["done"]
    assert (await world.stories.roadmap_of(PROJECT, "F1")).stories == ("S1", "S2")


async def test_work_that_waited_for_a_proposal_to_be_decided_is_announced_again(
    planning: Planning,
) -> None:
    analyst = Reviewer(proposing(S1))
    worker = await planning.worker(analyst)
    plan = await planning.owed()
    await planning.world.revise(STANDARD_RATE)
    assert (await worker.carry_out(plan)).startswith("refused: a proposal for this roadmap")
    await planning.world.column("UPDATE outbox SET published_at = now() RETURNING 1")

    await planning.world.decide(await planning.gate(), "request_changes", reason="not yet")

    assert await planning.world.column(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL "
        f"AND message_id = '{plan['command_message_id']}'"
    ) == [1]
    assert await planning.states() == ["wanted"]


async def test_a_proposal_sent_back_before_its_call_was_marked_applied_is_still_owed_again(
    planning: Planning,
) -> None:
    """3B-S3-1.3: what a proposal was the reply to is written with the proposal."""
    analyst = Reviewer(proposing(S1, S2))
    worker = await planning.worker(analyst)
    first = await planning.owed()
    assert await worker.carry_out(first) == "applied"
    # As if the process had stopped between the effect's mark and the invocation's.
    await planning.world.column("UPDATE invocations SET state = 'returned' RETURNING 1")

    await planning.world.decide(await planning.gate(), "request_changes", reason="too big")
    await planning.invocations.recover(PROJECT)

    assert await planning.states() == ["done", "wanted"]
    assert await planning.world.column(
        "SELECT envelope->'payload'->>'reason' FROM outbox o JOIN assignments a "
        "ON a.command_message_id = o.message_id WHERE a.succeeds IS NOT NULL"
    ) == ["your proposal was sent back: too big"]


async def test_a_proposal_that_could_not_be_approved_as_it_stood_ends_and_can_be_replaced(
    planning: Planning,
) -> None:
    """3B-S3-1.4: left waiting, it would refuse the very work owed to replace it."""
    world = planning.world
    await world.decide(await world.revise(STANDARD_RATE, GREETS))
    world.requests += 1
    await world.stories.reject(
        PROJECT,
        HumanAction(
            PRODUCT_OWNER,
            f"reject-{world.requests}",
            "S1",
            "reject",
            {"reason": "VAT is out of scope", "revision": "1", "basis": "0"},
        ),
    )
    reworded = {**S2, "change": "reworded", "title": "A warmer greeting"}
    kept, corrected = {"change": "kept", "story_id": "S1"}, {**S1, "story_id": "S3"}
    analyst = Reviewer(
        proposing(kept, reworded, {**corrected, "corrects": "S1"}),
        proposing(kept, {"change": "kept", "story_id": "S2"}, {**corrected, "corrects": "S1"}),
    )
    worker = await planning.worker(analyst)
    assert await worker.carry_out(await planning.owed("correct")) == "applied"
    # The story the proposal rewords moves on before the proposal is approved.
    await world.column(
        "UPDATE stories SET revision = revision + 1 WHERE story_id = 'S2' RETURNING 1"
    )

    assert (await world.decide(await planning.gate())).disposition == "stale"

    assert await world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "approved",
        "sent_back",
    ]
    assert await planning.states("correct") == ["done", "wanted"]
    assert await worker.carry_out(await planning.owed("correct")) == "applied"
    assert "could not be approved as it stood" in analyst.asked[1].briefing
    await world.decide(await planning.gate())
    assert (await world.stories.roadmap_of(PROJECT, "F1")).stories == ("S1", "S2", "S3")


async def test_a_roles_profile_caps_what_one_call_may_cost(planning: Planning) -> None:
    """3B-S4-1.10: carried from the profile to the call, which the adapter passes to the
    CLI as its own budget."""
    planning.task._profile = Profile("claude-haiku-5-5", budget_usd=Decimal("0.25"))
    owed = await planning.owed()

    call = await planning.task.prepare(owed, uuid.uuid7(), None)
    planning.task.discard(call)

    assert call.budget_usd == Decimal("0.25")


async def test_a_call_that_could_not_be_prepared_leaves_no_workspace_behind(
    planning: Planning, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-3.5: the repository cannot be reached, or the role's definition is missing
    once the trunk has been exported. No call comes of it, so nobody else knows of the
    workspace: it is cleared where it was made, each time the work is announced again."""
    from relay.application import authoring
    from relay.git.checkout import RemoteUnavailable

    root = tmp_path.resolve() / "workspaces"
    owed = await planning.owed()
    grant = uuid.uuid4()
    real = authoring.resolve_ref

    async def unreachable(*_: Any, **__: Any) -> str | None:
        raise RemoteUnavailable("no route to host")

    monkeypatch.setattr(authoring, "resolve_ref", unreachable)
    for _ in range(2):
        with pytest.raises(RemoteUnavailable):
            await planning.task.prepare(owed, grant, None)
    assert not root.exists() or list(root.iterdir()) == []

    monkeypatch.setattr(authoring, "resolve_ref", real)
    monkeypatch.setattr(planning.task, "_definitions", [tmp_path / "no-definitions-here"])
    monkeypatch.setattr(authoring, "find_definition", _missing)
    with pytest.raises(LookupError):
        await planning.task.prepare(owed, grant, None)
    assert list(root.iterdir()) == []

    monkeypatch.undo()
    call = await planning.task.prepare(owed, grant, None)
    assert [each.name for each in root.iterdir()] == [call.workdir.parent.name]


def _missing(*_: Any, **__: Any) -> Any:
    raise LookupError("no definition for this role")
