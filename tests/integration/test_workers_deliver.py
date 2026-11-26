"""A feature delivered by workers: from the stated problem to a completed feature, with
nothing called but each role's worker, the pass, and a person's decisions at the gates.
The providers are scripted; what is real is everything around them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relay.application.authoring import (
    AssessTask,
    ReconfirmTask,
    RoadmapTask,
    SealTask,
    SpecifyTask,
)
from relay.application.features import Features
from relay.application.invocations import Invocations
from relay.application.tasks import BuildTask, Profile, ReviewTask, Tasks, Workspaces
from relay.application.worker import Worker
from relay.application.workers import WorkerSession
from relay.providers.claude import ProviderResult, Request
from tests.integration.completion_world import CompletionWorld
from tests.integration.fixtures import PRODUCT_OWNER, VAT_APPLIES_STANDARD_RATE
from tests.integration.test_review_task import NOTHING_FOUND, replying
from tests.integration.test_roadmap_task import S1, proposing
from tests.integration.test_worker import RUNTIME, SeenToHold
from tests.integration.trunk_world import PROJECT

AGENTS = (
    "alf-user-story-writer",
    "alf-atdd-developer",
    "alf-test-design-reviewer",
    "alf-security-assessor",
)
STANDARD_RATE_CONTRACT = {
    "contract_id": "B1-standard-rate",
    "observes": "the gross amount printed for a net amount at the standard rate",
    "argv": ["python3", "vat.py", "100"],
    "timeout_seconds": 30,
    "expect_exit_code": 0,
    "expect_stdout": "120\n",
    "baseline_exit_code": 0,
    "baseline_stdout": "100\n",
}


def delivering(**delivered: Any) -> dict[str, Any]:
    return {"outcome": "delivered", "assumptions": [], "delivered": delivered}


B1 = delivering(
    demonstrated=False,
    behaviour_id="B1",
    summary="the standard rate is applied",
    demonstrates=["AC1"],
    contract=STANDARD_RATE_CONTRACT,
)
DEMONSTRATED = delivering(demonstrated=True)
VOUCHED = delivering(statement="B1 shows 120 for 100, which is AC1")


class Role:
    """A provider for one role: for each call it is told what kind of work it is by the
    briefing's own words, and gives the next reply it has for that."""

    def __init__(self, **replies: list[Any]) -> None:
        self.replies = replies
        self.asked: list[Request] = []

    async def invoke(self, request: Request, **_: Any) -> ProviderResult:
        self.asked.append(request)
        for marker, waiting in self.replies.items():
            if marker.replace("_", " ") in request.briefing:
                assert waiting, f"nothing more scripted for {marker!r}: {request.briefing[-600:]}"
                reply = waiting.pop(0)
                if callable(reply):
                    reply = reply(request)
                return replying(reply)
        raise AssertionError(f"nothing scripted for: {request.briefing[:200]}")


def builds(files: dict[str, str]) -> Any:
    def build(request: Request) -> dict[str, str]:
        for name, content in files.items():
            (request.workdir / name).write_text(content)
        return {"note": "applied the standard rate"}

    return build


class Delivering:
    def __init__(self, world: CompletionWorld, tmp_path: Path) -> None:
        self.world = world
        agents = tmp_path / "agents"
        agents.mkdir()
        for name in AGENTS:
            (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\nDo the work.\n")
        self.invocations = Invocations(world.database)
        profile = Profile("claude-sonnet-5-5")
        workspaces = Workspaces(tmp_path.resolve() / "workspaces")
        common = (world.database, self.invocations, workspaces, world.mirrors, [agents], profile)
        roadmap = RoadmapTask(*common)
        build = BuildTask(
            world.database,
            world.behaviours,
            self.invocations,
            workspaces,
            world.mirrors,
            [agents],
            profile,
        )
        review = ReviewTask(
            world.database, world.reviews, self.invocations, workspaces, [agents], profile
        )
        self.by_role: dict[str, dict[str, Any]] = {
            "analyst": {
                "plan": roadmap,
                "revise": roadmap,
                "correct": roadmap,
                "assess": AssessTask(*common),
                "reconfirm": ReconfirmTask("analyst", world.decisions, *common),
            },
            "verifier": {
                "specify": SpecifyTask(*common),
                "seal": SealTask(*common),
                "reconfirm": ReconfirmTask("verifier", world.decisions, *common),
            },
            "builder": {"build": build, "remediate": build},
            "qa": {"review": review},
            "sentinel": {"review": review},
        }
        every: dict[Any, Any] = {}
        for role, tasks in self.by_role.items():
            for kind, task in tasks.items():
                every[(kind, role) if kind == "reconfirm" else kind] = task
        Tasks(every).attach(self.invocations)
        world.features = Features(
            world.database,
            world.reviews,
            world.integration,
            invocations=self.invocations,
            behaviours=world.behaviours,
        )
        world.decisions._features = world.features
        self.workers: dict[str, Worker] = {}
        self.sessions: list[WorkerSession] = []
        self.left: list[str] = []

    async def staff(self, **providers: Any) -> None:
        for role, provider in providers.items():
            session = await WorkerSession.start(self.world.database, role)
            self.sessions.append(session)
            self.workers[role] = Worker(
                session,
                self.world.claims,
                self.invocations,
                provider,
                self.by_role[role],
                SeenToHold(),
                lambda: RUNTIME,
                then=self.world.features.reconcile,
            )

    async def work(self) -> None:
        """Every worker takes up whatever is wanted of it, until nothing more comes of
        that. Nobody but a worker and the pass does anything here."""
        tried: set[tuple[object, str]] = set()
        while True:
            async with self.world.database.connection() as connection:
                wanted = await (
                    await connection.execute(
                        "SELECT * FROM assignments WHERE project_id = %s AND state = 'wanted' "
                        "AND role = ANY(%s) ORDER BY created_at",
                        (PROJECT, list(self.workers)),
                    )
                ).fetchall()
            fresh = [each for each in wanted if (each["assignment_id"], "wanted") not in tried]
            if not fresh:
                return
            for assignment in fresh:
                tried.add((assignment["assignment_id"], "wanted"))
                left = await self.workers[assignment["role"]].carry_out(assignment)
                self.left.append(f"{assignment['kind']}: {left}")

    async def done(self) -> list[object]:
        return await self.world.column(
            "SELECT kind FROM assignments WHERE state = 'done' ORDER BY created_at"
        )


async def test_a_feature_is_delivered_by_workers_and_a_persons_decisions_alone(
    delivering_world: Delivering,
) -> None:
    """S3-18.7, S3-19.8, S3-19.9: nothing here publishes a candidate, asks for a red or
    an integration, or runs a pass of its own."""
    at, world = delivering_world, delivering_world.world
    await at.staff(
        analyst=Role(You_propose_the_roadmap=[proposing(S1)], You_are_asked_to_assess=[VOUCHED]),
        verifier=Role(
            You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED],
            You_are_asked_to_vouch=[VOUCHED],
        ),
        builder=Role(You_are_the_Builder=[builds({"vat.py": VAT_APPLIES_STANDARD_RATE})]),
        qa=Role(You_are_reviewing=[NOTHING_FOUND]),
        sentinel=Role(You_are_reviewing=[NOTHING_FOUND]),
    )

    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    [roadmap] = await world.stories.inbox(PROJECT)
    assert roadmap.kind == "roadmap"
    await world.decide(roadmap.gate_id)
    await at.work()

    [story] = await world.stories.inbox(PROJECT)
    assert (story.kind, story.story_id) == ("story", "S1"), at.left
    assert story.manifest["assessment"]["statement"] == "B1 shows 120 for 100, which is AC1"
    on_trunk = world.git("show", f"{world.trunk()}:vat.py", at=world.remote)
    assert on_trunk == VAT_APPLIES_STANDARD_RATE.strip()
    await world.decide(story.gate_id)
    await at.work()

    assert await world.feature() == "completed", at.left
    assert await at.done() == [
        "plan",
        "specify",
        "red",
        "build",
        "review",
        "review",
        "integrate",
        "specify",
        "seal",
        "assess",
        "complete",
    ]
    assert await world.column(
        "SELECT count(*) FROM assignments WHERE state IN ('wanted', 'granted')"
    ) == [0]
