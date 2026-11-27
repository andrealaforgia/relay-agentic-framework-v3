"""A review carried out by a worker: QA and Sentinel are each handed their own export of
the pinned candidate, briefed from the record, and their replies become recorded reviews.
The provider is scripted; what is real is everything around it."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.invocations import Invocations
from relay.application.reviews import NewFinding, Review
from relay.application.tasks import Profile, ReviewTask, Tasks, Workspaces
from relay.application.worker import Worker
from relay.application.workers import WorkerSession
from relay.domain.review import Severity
from relay.domain.story import ActionRefused
from relay.providers.claude import Outcome, ProviderResult, Request
from relay.providers.stream import Terminal
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    run_green,
)
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_worker import RUNTIME, SeenToHold
from tests.integration.trunk_world import PROJECT

NOTHING_FOUND = {"findings": [], "carried": []}
NO_USAGE_MESSAGE = {
    "severity": "low",
    "claim": "no usage message",
    "evidence": "vat.py line 1",
    "remediation": "print usage on bad input",
}


_ITS_OWN = {"findings", "carried", "note"}


def as_offered(reply: Any) -> Any:
    """A scripted build's or review's reply, written in these tests as what it delivers
    or asks, in the form every call is offered: `outcome`, with what was delivered under
    `delivered`. A reply already in that form, or one that is nothing like either, is
    left as it is."""
    if not isinstance(reply, dict) or {"outcome", "delivered"} & set(reply):
        return reply
    if not ((_ITS_OWN | {"question"}) & set(reply)):
        return reply
    said = {name: value for name, value in reply.items() if name in ("question", "assumptions")}
    said.setdefault("assumptions", [])
    rest = {name: value for name, value in reply.items() if name not in said}
    if "question" in reply and not rest:
        return {"outcome": "question", **said}
    return {"outcome": "delivered", **said, "delivered": rest}


def replying(reply: Any) -> ProviderResult:
    reply = as_offered(reply)
    return ProviderResult(
        Outcome.RETURNED,
        Terminal(
            {"type": "result", "subtype": "success", "is_error": False, "structured_output": reply}
        ),
    )


class Reviewer:
    """A provider that looks at what it was handed before it replies."""

    def __init__(self, *replies: Any) -> None:
        self._replies = list(replies)
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
        return replying(self._replies.pop(0))


class Reviewing:
    def __init__(self, world: DecisionWorld, tmp_path: Path) -> None:
        self.world = world
        agents = tmp_path / "agents"
        agents.mkdir()
        for name in ("alf-test-design-reviewer", "alf-security-assessor"):
            (agents / f"{name}.md").write_text(f"---\nname: {name}\ntools: Bash\n---\nReview.\n")
        self.invocations = Invocations(world.database)
        self.task = ReviewTask(
            world.database,
            world.reviews,
            self.invocations,
            Workspaces(tmp_path.resolve() / "workspaces"),
            [agents],
            Profile("claude-sonnet-5-5"),
        )
        Tasks({"review": self.task}).attach(self.invocations)
        self.sessions: list[WorkerSession] = []
        self.candidate = ""

    async def worker(self, role: str, provider: Any) -> Worker:
        session = await WorkerSession.start(self.world.database, role)
        self.sessions.append(session)
        return Worker(
            session,
            self.world.claims,
            self.invocations,
            provider,
            {"review": self.task},
            SeenToHold(),
            lambda: RUNTIME,
        )

    async def owed(self, role: str) -> dict[str, Any]:
        async with self.world.database.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE kind = 'review' AND role = %s "
                    "AND project_id = %s ORDER BY created_at DESC LIMIT 1",
                    (role, PROJECT),
                )
            ).fetchone()
        assert row is not None
        return row


@pytest.fixture
async def reviewing(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Reviewing]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    reviewing = Reviewing(world, tmp_path)
    reviewing.candidate = world.publish_attempt(
        "B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base
    )
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    yield reviewing
    await reviewing.invocations.close()
    for session in reviewing.sessions:
        await session.stop()


async def test_a_reviewer_is_handed_the_candidate_and_its_reply_becomes_a_review(
    reviewing: Reviewing,
) -> None:
    provider = Reviewer({"findings": [NO_USAGE_MESSAGE], "carried": []})
    worker = await reviewing.worker("qa", provider)

    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"

    [asked] = provider.asked
    [tree] = provider.found
    assert tree["vat.py"] == VAT_APPLIES_STANDARD_RATE
    assert not any(name.startswith(".git") for name in tree)  # the tree, and no metadata
    assert asked.confinement.tools == ("Read", "Grep", "Glob")
    assert asked.definition.name == "alf-test-design-reviewer"
    assert asked.model == "claude-sonnet-5-5"
    assert reviewing.candidate in asked.briefing
    async with reviewing.world.database.connection() as connection:
        finding = await (await connection.execute("SELECT * FROM findings")).fetchone()
        submitted = await (
            await connection.execute("SELECT disposition FROM review_submissions")
        ).fetchall()
    assert finding is not None
    assert (finding["role"], finding["severity"], finding["claim"]) == ("qa", 1, "no usage message")
    assert [each["disposition"] for each in submitted] == ["effective"]
    assert (await reviewing.owed("qa"))["state"] == "done"


async def test_the_briefing_says_what_is_being_reviewed_and_for_what(reviewing: Reviewing) -> None:
    provider = Reviewer(NOTHING_FOUND)
    await (await reviewing.worker("sentinel", provider)).carry_out(await reviewing.owed("sentinel"))

    briefing = provider.asked[0].briefing
    assert "security" in briefing
    assert "behaviour B1" in briefing and "story S1" in briefing
    assert "AC1" in briefing
    assert "the gross amount printed" in briefing  # the contract, as registered
    assert "no earlier findings" in briefing
    assert provider.asked[0].definition.name == "alf-security-assessor"


async def test_the_two_reviewers_each_get_their_own_copy_of_the_same_commit(
    reviewing: Reviewing,
) -> None:
    """S3-1.10: neither can leave anything for the other in the tree."""
    qa, sentinel = Reviewer(NOTHING_FOUND), Reviewer(NOTHING_FOUND)

    await (await reviewing.worker("qa", qa)).carry_out(await reviewing.owed("qa"))
    await (await reviewing.worker("sentinel", sentinel)).carry_out(await reviewing.owed("sentinel"))

    assert qa.found == sentinel.found
    assert qa.found[0]["vat.py"] == VAT_APPLIES_STANDARD_RATE
    assert qa.asked[0].workdir != sentinel.asked[0].workdir
    assert await reviewing.world.column("SELECT state FROM review_cycles") == ["satisfied"]


async def test_a_reply_that_is_not_a_review_is_asked_for_again(reviewing: Reviewing) -> None:
    not_a_review = {"findings": [{**NO_USAGE_MESSAGE, "severity": "enormous"}], "carried": []}
    provider = Reviewer(not_a_review, NOTHING_FOUND)
    worker = await reviewing.worker("qa", provider)

    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"

    assert await reviewing.world.column("SELECT state FROM invocations ORDER BY attempt") == [
        "rejected",
        "applied",
    ]
    assert "could not be used" in provider.asked[1].briefing
    assert await reviewing.world.column("SELECT count(*) FROM findings") == [0]
    # Turned away when it was recorded, before any of it was applied.
    assert await reviewing.world.column(
        "SELECT count(*) FROM invocation_effects e JOIN invocations v USING (invocation_id) "
        "WHERE v.attempt = 1"
    ) == [0]


async def test_a_verdict_on_a_finding_that_is_not_the_reviewers_own_is_refused_by_the_service(
    reviewing: Reviewing,
) -> None:
    """The reply is well formed; the review service refuses it. That is a reply that gave
    nothing, and the next briefing says why."""
    sentinel = Reviewer({"findings": [NO_USAGE_MESSAGE], "carried": []})
    await (await reviewing.worker("sentinel", sentinel)).carry_out(await reviewing.owed("sentinel"))
    [theirs] = await reviewing.world.column("SELECT finding_id FROM findings")
    overreaching = {
        "findings": [],
        "carried": [{"finding_id": theirs, "verdict": "verified_fixed"}],
    }
    provider = Reviewer(overreaching, NOTHING_FOUND)

    worker = await reviewing.worker("qa", provider)
    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"

    assert await reviewing.world.column(
        "SELECT state FROM invocations WHERE role = 'qa' ORDER BY attempt"
    ) == ["rejected", "applied"]
    assert await reviewing.world.column("SELECT count(*) FROM finding_verifications") == [0]


async def test_a_review_whose_worker_went_after_it_replied_is_submitted_by_the_pass_once(
    reviewing: Reviewing,
) -> None:
    """S3-18.2: under its original grant, from its original checkout."""
    provider = Reviewer({"findings": [NO_USAGE_MESSAGE], "carried": []})
    worker = await reviewing.worker("qa", provider)
    real = reviewing.invocations.apply

    async def the_worker_stops(*_: Any) -> str:
        raise KeyboardInterrupt

    reviewing.invocations.apply = the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await reviewing.owed("qa"))
    reviewing.invocations.apply = real  # type: ignore[method-assign]
    await reviewing.sessions[-1].stop()

    await reviewing.invocations.recover(PROJECT)
    await reviewing.invocations.recover(PROJECT)

    assert await reviewing.world.column("SELECT state FROM invocations") == ["applied"]
    assert await reviewing.world.column("SELECT count(*) FROM findings") == [1]
    assert await reviewing.world.column("SELECT count(*) FROM review_checkouts") == [1]
    assert len(provider.asked) == 1


async def test_a_reviewers_workspace_is_one_the_bounds_hold_for(reviewing: Reviewing) -> None:
    provider = Reviewer(NOTHING_FOUND)
    await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa"))

    workdir = provider.asked[0].workdir
    assert workdir.is_absolute() and workdir.name == "tree"
    assert not workdir.is_relative_to(Path.home())
    assert uuid.UUID(workdir.parent.name)  # a directory of its own
    assert not workdir.parent.exists()  # and gone once the review is finished with


async def test_a_reviewer_is_asked_how_its_earlier_findings_stand_and_its_answer_is_recorded(
    reviewing: Reviewing,
) -> None:
    """A second candidate after the first was sent back: the reviewer's own finding is in
    its briefing, and what it says of it is what the gate goes by."""
    world = reviewing.world
    blocker = {**NO_USAGE_MESSAGE, "severity": "high", "claim": "the amount is passed to eval"}
    await (
        await reviewing.worker("qa", Reviewer({"findings": [blocker], "carried": []}))
    ).carry_out(await reviewing.owed("qa"))
    await (await reviewing.worker("sentinel", Reviewer(NOTHING_FOUND))).carry_out(
        await reviewing.owed("sentinel")
    )
    [raised] = await world.column("SELECT finding_id FROM findings")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE + "# parsed\n"}, on=world.base)
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    provider = Reviewer(
        {"findings": [], "carried": [{"finding_id": raised, "verdict": "verified_fixed"}]}
    )

    assert await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa")) == (
        "applied"
    )

    briefing = provider.asked[0].briefing
    assert str(raised) in briefing and "the amount is passed to eval" in briefing
    assert "verified_fixed, still_present, or withdrawn" in briefing
    assert await world.column("SELECT finding_id FROM finding_verifications") == [raised]


async def test_a_review_is_submitted_from_the_checkout_the_model_was_handed(
    reviewing: Reviewing,
) -> None:
    """3B-S2-1.7: the receipt is recorded with the call, and a later checkout under the
    same grant does not take its place."""
    provider = Reviewer(NOTHING_FOUND)
    worker = await reviewing.worker("qa", provider)
    real = reviewing.invocations.apply

    async def the_worker_stops(*_: Any) -> str:
        raise KeyboardInterrupt

    reviewing.invocations.apply = the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await reviewing.owed("qa"))
    reviewing.invocations.apply = real  # type: ignore[method-assign]
    [handed] = await reviewing.world.column("SELECT receipt_id FROM review_checkouts")
    owed = await reviewing.owed("qa")
    # Another checkout under the same grant, as a duplicate preparation would make.
    await reviewing.task.prepare(owed, owed["grant_id"], None)

    await reviewing.invocations.recover(PROJECT)

    assert await reviewing.world.column("SELECT evidence->>'checkout' FROM invocations") == [
        str(handed)
    ]
    assert await reviewing.world.column("SELECT state FROM invocations") == ["applied"]
    assert await reviewing.world.column(
        "SELECT count(*) FROM review_submissions WHERE disposition = 'effective'"
    ) == [1]


async def test_a_review_that_arrives_when_another_was_accepted_is_overtaken_and_blamed_for_nothing(
    reviewing: Reviewing,
) -> None:
    """3B-S2-1.7: the service keeps it as what was said and it decides nothing. That is
    not an applied review, and not a defective reply either."""
    provider = Reviewer({"findings": [NO_USAGE_MESSAGE], "carried": []})
    worker = await reviewing.worker("qa", provider)
    real = reviewing.invocations.apply

    async def the_worker_stops(*_: Any) -> str:
        raise KeyboardInterrupt

    reviewing.invocations.apply = the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await reviewing.owed("qa"))
    reviewing.invocations.apply = real  # type: ignore[method-assign]
    owed = await reviewing.owed("qa")
    [task_id] = await reviewing.world.column("SELECT task_id FROM review_tasks WHERE role = 'qa'")
    [receipt] = await reviewing.world.column("SELECT receipt_id FROM review_checkouts")
    # Another review of the same task is accepted first, with other content.
    await reviewing.world.reviews.submit_review(task_id, Review(), owed["grant_id"], receipt)

    await reviewing.invocations.recover(PROJECT)

    assert await reviewing.world.column("SELECT state FROM invocations") == ["overtaken"]
    assert await reviewing.world.column("SELECT count(*) FROM findings") == [0]
    assert await reviewing.invocations.parked(PROJECT) == []


async def test_a_reviewer_is_not_briefed_on_another_projects_findings(
    reviewing: Reviewing, database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    """3B-S2-1.5: two projects each have a behaviour called B1."""
    elsewhere = DecisionWorld(database, tmp_path / "elsewhere", runner_image)
    elsewhere.project = "billing"
    (tmp_path / "elsewhere").mkdir()
    elsewhere.base = await elsewhere.open()
    await elsewhere.decide(await elsewhere.roadmap(STANDARD_RATE))
    base = await elsewhere.awaiting_builder("B1")
    theirs = NewFinding(Severity.HIGH, "billing leaks card numbers", "pay.py", "mask them")
    await elsewhere.reviewed("B1", on=base, reviews={"qa": Review(findings=(theirs,))})
    provider = Reviewer(NOTHING_FOUND)

    await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa"))

    briefing = provider.asked[0].briefing
    assert "billing leaks card numbers" not in briefing
    assert "no earlier findings" in briefing


async def test_a_reply_with_the_wrong_kinds_of_value_in_it_becomes_no_finding(
    reviewing: Reviewing,
) -> None:
    """3B-S2-1.11: a number for a claim, nothing for the evidence, true for the remedy."""
    malformed = {
        "findings": [{"severity": "high", "claim": 17, "evidence": None, "remediation": True}],
        "carried": [],
    }
    provider = Reviewer(malformed, NOTHING_FOUND)
    worker = await reviewing.worker("qa", provider)

    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"

    assert await reviewing.world.column("SELECT state FROM invocations ORDER BY attempt") == [
        "rejected",
        "applied",
    ]
    assert await reviewing.world.column("SELECT count(*) FROM findings") == [0]
    assert await reviewing.world.column(
        "SELECT envelope IS NOT NULL FROM invocations WHERE attempt = 1"
    ) == [True]  # the paid call is kept


async def test_only_the_builder_disputes_a_finding(reviewing: Reviewing) -> None:
    """3B-S2-1.10: enforced where the service is called, whoever holds the service."""
    provider = Reviewer({"findings": [NO_USAGE_MESSAGE], "carried": []})
    await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa"))
    [raised] = await reviewing.world.column("SELECT finding_id FROM findings")

    with pytest.raises(ActionRefused, match="for the builder"):
        await reviewing.world.reviews.dispute(VERIFIER, PROJECT, str(raised), "it is fine")

    assert await reviewing.world.column("SELECT status FROM findings") == ["open"]


async def test_a_malformed_review_handed_straight_to_the_record_becomes_no_finding(
    reviewing: Reviewing,
) -> None:
    """3B-S2-2.6: a result that reaches the record by other hands than the worker's is
    checked there, against the schema stored with its call. What it used is kept; no
    finding is made of it, by the pass or by anyone."""
    await reviewing.worker("qa", Reviewer())
    session = reviewing.sessions[-1]
    owed = await reviewing.owed("qa")
    grant = await reviewing.world.claims.claim(
        session.principal, owed["assignment_id"], session.worker_id
    )
    call = await reviewing.task.prepare(owed, grant, None)
    admission = await reviewing.invocations.admit(
        session, owed["assignment_id"], grant, call.prepared
    )
    assert admission.invocation_id is not None
    malformed = replying({"findings": [{**NO_USAGE_MESSAGE, "claim": 7}], "carried": []})

    assert await reviewing.invocations.record(admission.invocation_id, malformed) == "rejected"
    await reviewing.invocations.recover(PROJECT)

    assert await reviewing.world.column("SELECT count(*) FROM findings") == [0]
    assert await reviewing.world.column("SELECT count(*) FROM review_submissions") == [0]
    [reason] = await reviewing.world.column("SELECT reason FROM invocations")
    assert reason == "claim of item 1 of findings of delivered of the reply is not a string"
    [kept] = await reviewing.world.column(
        "SELECT envelope->'structured_output'->'delivered'->'findings'->0->>'claim' FROM invocations"
    )
    assert kept == "7"


async def test_no_tree_is_left_behind_by_a_review_or_by_a_delivery_that_came_to_nothing(
    reviewing: Reviewing,
) -> None:
    """3B-S2-3.8: every preparation exports a tree, and a worker given the same review
    again and again while it may launch nothing would otherwise keep every one."""
    root = reviewing.task._workspaces._root
    worker = await reviewing.worker("qa", Reviewer(NOTHING_FOUND))
    worker._boundaries.holding = False  # type: ignore[attr-defined]

    for _ in range(3):
        assert (await worker.carry_out(await reviewing.owed("qa"))).startswith("refused")
    assert list(root.iterdir()) == []

    worker._boundaries.holding = True  # type: ignore[attr-defined]
    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"
    assert list(root.iterdir()) == []


async def test_one_reviewer_asking_while_the_other_reviews_does_not_stop_either(
    reviewing: Reviewing,
) -> None:
    """3B-S3-2.2: the gate is taken exclusively before the behaviour's row, by both, so
    neither waits on the other while holding what the other needs."""
    asks = {
        "question": {
            "text": "Should a negative amount be refused?",
            "why": "no criterion says",
            "options": ["yes", "no"],
        }
    }
    qa = await reviewing.worker("qa", Reviewer(asks))
    sentinel = await reviewing.worker("sentinel", Reviewer(NOTHING_FOUND))

    for _ in range(5):  # whichever order their transactions fall in
        left = await asyncio.gather(
            qa.carry_out(await reviewing.owed("qa")),
            sentinel.carry_out(await reviewing.owed("sentinel")),
        )
        if left[0] == "applied":
            break

    assert left[0] == "applied" and left[1] in ("applied", "held")
    assert len(await reviewing.world.decisions.open_questions(PROJECT)) == 1


async def test_what_a_reviewer_assumed_is_on_record_with_the_review_it_delivered(
    reviewing: Reviewing,
) -> None:
    """3B-S3-2.7: in the transaction that accepts the review."""
    reply = {**NOTHING_FOUND, "assumptions": ["amounts are whole numbers"]}
    worker = await reviewing.worker("qa", Reviewer(reply))

    assert await worker.carry_out(await reviewing.owed("qa")) == "applied"

    assert await reviewing.world.column("SELECT text FROM decisions") == [
        "amounts are whole numbers"
    ]
    assert await reviewing.world.column("SELECT count(*) FROM review_submissions") == [1]


async def a_second_candidate_with_an_earlier_finding(reviewing: Reviewing) -> Any:
    """QA raised a blocking finding on the first candidate, and a second is published."""
    world = reviewing.world
    blocker = {**NO_USAGE_MESSAGE, "severity": "high", "claim": "the amount is passed to eval"}
    await (
        await reviewing.worker("qa", Reviewer({"findings": [blocker], "carried": []}))
    ).carry_out(await reviewing.owed("qa"))
    await (await reviewing.worker("sentinel", Reviewer(NOTHING_FOUND))).carry_out(
        await reviewing.owed("sentinel")
    )
    [raised] = await world.column("SELECT finding_id FROM findings")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE + "# parsed\n"}, on=world.base)
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    return raised


@pytest.mark.parametrize("last", ["verified_fixed", "still_present"])
async def test_two_verdicts_on_one_finding_decide_nothing_whichever_comes_last(
    reviewing: Reviewing, last: str
) -> None:
    """3B-S4-1.4: which was meant is not for the order of a list to say."""
    raised = await a_second_candidate_with_an_earlier_finding(reviewing)
    first = "still_present" if last == "verified_fixed" else "verified_fixed"
    both = [{"finding_id": raised, "verdict": first}, {"finding_id": raised, "verdict": last}]
    provider = Reviewer(
        {"findings": [], "carried": both},
        {"findings": [], "carried": [{"finding_id": raised, "verdict": "still_present"}]},
    )

    await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa"))

    assert "are given more than one verdict: one each" in provider.asked[1].briefing
    assert await reviewing.world.column("SELECT count(*) FROM finding_verifications") == [0]


async def test_a_review_that_says_nothing_of_a_finding_its_reviewer_was_asked_about_is_no_review(
    reviewing: Reviewing,
) -> None:
    """3B-S4-1.7: leaving the list out is saying nothing, and it had been asked."""
    raised = await a_second_candidate_with_an_earlier_finding(reviewing)
    provider = Reviewer(
        {"findings": []},
        {"findings": [], "carried": [{"finding_id": raised, "verdict": "still_present"}]},
    )
    submitted = await reviewing.world.column("SELECT count(*) FROM review_submissions")

    await (await reviewing.worker("qa", provider)).carry_out(await reviewing.owed("qa"))

    assert f"the review says nothing of ['{raised}'], which you raised before" in (
        provider.asked[1].briefing
    )
    assert await reviewing.world.column("SELECT state FROM invocations ORDER BY started_at") == [
        "applied",
        "applied",
        "rejected",
        "applied",
    ]
    [now] = await reviewing.world.column("SELECT count(*) FROM review_submissions")
    assert now == submitted[0] + 1  # the one that answered, and not the one that did not
