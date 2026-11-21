"""Scenario 6: QA and Sentinel independently review the same immutable candidate, and
unresolved blocking findings prevent completion. Reviewers here are scripted callers; what
is under test is routing, authority and the gate, not the quality of a review."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import reviews as reviews_module
from relay.application.behaviours import Behaviours
from relay.application.reviews import NewFinding, Review, Reviews, UnknownCapability
from relay.domain.behaviour import BehaviourState
from relay.domain.review import Carried, Severity, SubmissionRefused
from relay.git import mirror as mirror_module
from relay.git.checkout import CheckoutFailed, RemoteUnavailable
from relay.git.mirror import Mirror
from relay.persistence.ledger import read_events
from relay.persistence.outbox import OutboxPublisher, pending_messages
from relay.persistence.receipts import act_once
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from relay.transport.streams import Streams
from tests.integration.fixtures import (
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    announce_wanted_work_again,
    commit,
    in_a_story,
    prepare_review,
    publish_attempt,
    run_green,
    submit_review,
)

PROJECT = "vat-calculator"
INJECTION = NewFinding(
    severity=Severity.HIGH,
    claim="the amount is passed to eval",
    evidence="vat.py line 2",
    remediation="parse it as an integer",
    affected_files=("vat.py",),
)
STYLE = NewFinding(Severity.LOW, "no usage message", "vat.py", "print usage on bad input")
APPROVE = Review()


class World:
    """One project with one behaviour, driven the way a Builder and two reviewers would."""

    def __init__(self, database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> None:
        self.database = database
        self.tmp_path = tmp_path
        self.repository = tmp_path / "subject"
        self.mirrors = tmp_path / "mirrors"
        self.behaviours = Behaviours(
            database, self.mirrors, ProcessOracle(ContainerIsolation(runner_image))
        )
        self.reviews = Reviews(database, self.mirrors)
        self.red_revision = ""
        self.attempts = 0

    async def release_to_builder(self) -> None:
        self.red_revision = commit(self.repository, {"vat.py": VAT_RETURNS_NET})
        await self.behaviours.open_project(PROJECT, str(self.repository))
        await in_a_story(self.behaviours, PROJECT)
        await self.behaviours.propose(
            PROJECT, "S1", "B1", "standard VAT rate is applied", ["AC1"], basis=0, story_revision=1
        )
        await self.behaviours.begin_test_design(PROJECT, "B1")
        await self.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)
        assert (await self.behaviours.run_red(PROJECT, "B1")).is_valid

    async def green_candidate(self, note: str = "") -> str:
        """The Builder publishes a passing attempt and Relay verifies it; no publication yet."""
        self.attempts += 1
        candidate = publish_attempt(
            self.repository,
            "B1",
            self.attempts,
            {"vat.py": VAT_APPLIES_STANDARD_RATE + f"# {note} {self.attempts}\n"},
            on=self.red_revision,
        )
        assert (await run_green(self.behaviours, PROJECT, "B1", attempt=self.attempts)).is_valid
        return candidate

    async def candidate_under_review(self, note: str = "") -> tuple[str, dict[str, str]]:
        """A verified, published candidate, and each reviewer's capability from its command."""
        candidate = await self.green_candidate(note)
        assert await self.reviews.publish_candidates() == 1
        return candidate, await self.capabilities(candidate)

    async def capabilities(self, candidate: str) -> dict[str, str]:
        return {
            message.envelope["recipient"]: message.envelope["payload"]["capability"]
            for message in await pending_messages(self.database)
            if message.envelope["event_type"] == "review.requested"
            and message.envelope["payload"]["candidate_revision"] == candidate
        }

    async def review(self, capability: str, review: Review) -> Any:
        tree = self.tmp_path / f"review-{capability[:8]}"
        shutil.rmtree(tree, ignore_errors=True)
        await prepare_review(self.reviews, capability, tree)
        return await submit_review(self.reviews, capability, review)

    async def state(self) -> BehaviourState:
        return await self.behaviours.state_of(PROJECT, "B1")

    async def count(self, table: str) -> int:
        async with self.database.connection() as connection:
            row = await (await connection.execute(f"SELECT count(*) AS n FROM {table}")).fetchone()
        assert row is not None
        return int(row["n"])

    async def cycle_states(self) -> list[str]:
        async with self.database.connection() as connection:
            rows = await (
                await connection.execute("SELECT state FROM review_cycles ORDER BY created_at")
            ).fetchall()
        return [row["state"] for row in rows]

    async def events(self) -> list[str]:
        return [event.event_type for event in await read_events(self.database, PROJECT)]

    def remote_ref(self, ref: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def move_remote_ref(self, ref: str, to: str) -> None:
        subprocess.run(["git", "-C", str(self.repository), "update-ref", ref, to], check=True)


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> World:
    world = World(database, tmp_path, runner_image)
    await world.release_to_builder()
    return world


async def candidate_ref_of(world: World) -> str:
    async with world.database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT candidate_ref FROM review_cycles ORDER BY created_at DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    return str(row["candidate_ref"])


async def test_both_reviewers_are_sent_the_same_published_commit(world: World) -> None:
    candidate, capabilities = await world.candidate_under_review()

    assert set(capabilities) == {"qa", "sentinel"}
    assert capabilities["qa"] != capabilities["sentinel"]
    assert world.remote_ref(await candidate_ref_of(world)) == candidate
    for role, capability in capabilities.items():
        tree = await prepare_review(world.reviews, capability, world.tmp_path / f"tree-{role}")
        assert (tree / "vat.py").read_text().startswith(VAT_APPLIES_STANDARD_RATE)
        assert sorted(entry.name for entry in tree.iterdir()) == ["vat.py"]


async def test_two_approvals_satisfy_the_gate_and_the_behaviour_stays_in_review(
    world: World,
) -> None:
    _, capabilities = await world.candidate_under_review()

    await world.review(capabilities["qa"], APPROVE)
    assert "review.gate_satisfied" not in await world.events()
    await world.review(capabilities["sentinel"], Review(findings=(STYLE,)))

    assert (await world.events())[-1] == "review.gate_satisfied"
    assert await world.cycle_states() == ["satisfied"]
    assert await world.state() is BehaviourState.REVIEW
    assert await world.count("findings") == 1


async def test_a_blocking_finding_returns_the_work_once_however_often_it_is_submitted(
    world: World,
) -> None:
    _, capabilities = await world.candidate_under_review()
    await world.review(capabilities["qa"], APPROVE)

    first = await world.review(capabilities["sentinel"], Review(findings=(INJECTION,)))
    replay = await submit_review(
        world.reviews, capabilities["sentinel"], Review(findings=(INJECTION,))
    )

    assert await world.state() is BehaviourState.IMPLEMENTING
    assert await world.cycle_states() == ["changes_requested"]
    assert (first.disposition, len(first.finding_ids), first.cycle_is_current) == (
        "effective",
        1,
        True,
    )
    assert (replay.submission_id, replay.finding_ids) == (first.submission_id, first.finding_ids)
    assert replay.cycle_is_current is False
    assert await world.count("findings") == 1
    assert await world.count("remediation_requests") == 1
    remediations = [
        message
        for message in await pending_messages(world.database)
        if "remediate_findings" in message.envelope["payload"]
    ]
    assert [m.envelope["payload"]["remediate_findings"] for m in remediations] == [
        list(first.finding_ids)
    ]


async def test_a_retry_that_says_something_different_is_kept_but_changes_nothing(
    world: World,
) -> None:
    _, capabilities = await world.candidate_under_review()
    await world.review(capabilities["qa"], APPROVE)
    first = await world.review(capabilities["sentinel"], Review(findings=(INJECTION,)))

    second_thoughts = await submit_review(world.reviews, capabilities["sentinel"], APPROVE)

    assert second_thoughts.disposition == "conflicting"
    assert second_thoughts.submission_id != first.submission_id
    assert await world.state() is BehaviourState.IMPLEMENTING
    assert await world.count("findings") == 1
    assert (await world.events())[-1] == "review.submission_conflicting"


async def test_the_fix_must_be_verified_on_the_successor_by_the_reviewer_who_raised_it(
    world: World,
) -> None:
    _, first = await world.candidate_under_review()
    await world.review(first["qa"], APPROVE)
    raised = await world.review(first["sentinel"], Review(findings=(INJECTION,)))
    [finding] = raised.finding_ids
    successor, second = await world.candidate_under_review("fixed")
    assert second.keys() == first.keys() and not set(second.values()) & set(first.values())

    late = await submit_review(world.reviews, first["qa"], APPROVE)
    await world.review(second["qa"], APPROVE)
    assert await world.cycle_states() == ["changes_requested", "open"]
    await world.review(second["sentinel"], Review(carried={finding: Carried.VERIFIED_FIXED}))

    assert late.cycle_is_current is False
    assert await world.cycle_states() == ["changes_requested", "satisfied"]
    assert await world.state() is BehaviourState.REVIEW
    assert world.remote_ref(await candidate_ref_of(world)) == successor


async def test_a_fix_seen_on_one_candidate_does_not_clear_the_next(world: World) -> None:
    """Three candidates, one finding. Verified fixed on B; QA wants more; C is reviewed
    by Sentinel without a word about the finding. C must not pass."""
    _, on_a = await world.candidate_under_review("a")
    await world.review(on_a["qa"], APPROVE)
    [finding] = (await world.review(on_a["sentinel"], Review(findings=(INJECTION,)))).finding_ids
    _, on_b = await world.candidate_under_review("b")
    await world.review(on_b["sentinel"], Review(carried={finding: Carried.VERIFIED_FIXED}))
    more = NewFinding(
        Severity.HIGH, "rounding is wrong for odd amounts", "101 gives 121", "round half up"
    )
    [qa_finding] = (await world.review(on_b["qa"], Review(findings=(more,)))).finding_ids
    assert await world.state() is BehaviourState.IMPLEMENTING
    _, on_c = await world.candidate_under_review("c")

    await world.review(on_c["qa"], Review(carried={qa_finding: Carried.VERIFIED_FIXED}))
    await world.review(on_c["sentinel"], APPROVE)

    assert await world.cycle_states() == ["changes_requested"] * 3
    assert await world.state() is BehaviourState.IMPLEMENTING
    last = (await read_events(world.database, PROJECT))[-1]
    assert (last.event_type, last.payload["blocking"]) == ("review.changes_requested", [finding])


async def test_a_reviewer_cannot_settle_another_reviewers_finding(world: World) -> None:
    _, first = await world.candidate_under_review()
    await world.review(first["qa"], APPROVE)
    [finding] = (await world.review(first["sentinel"], Review(findings=(INJECTION,)))).finding_ids
    _, second = await world.candidate_under_review("fixed")
    submissions = await world.count("review_submissions")

    for verdict in (Carried.VERIFIED_FIXED, Carried.WITHDRAWN):
        with pytest.raises(SubmissionRefused, match="raised by sentinel"):
            await world.review(
                second["qa"], Review(carried={finding: verdict}, withdrawal_reasons={finding: "x"})
            )

    assert await world.count("review_submissions") == submissions
    assert await world.count("finding_verifications") == 0
    assert await world.cycle_states() == ["changes_requested", "open"]


async def test_a_disputed_finding_still_blocks_until_its_reviewer_withdraws_it_with_a_reason(
    world: World,
) -> None:
    _, first = await world.candidate_under_review()
    await world.review(first["qa"], APPROVE)
    [finding] = (await world.review(first["sentinel"], Review(findings=(INJECTION,)))).finding_ids
    await world.reviews.dispute(PROJECT, finding, "the argument is parsed with int(), not eval")
    _, second = await world.candidate_under_review("unchanged")
    await world.review(second["qa"], APPROVE)

    with pytest.raises(SubmissionRefused, match="needs a reason"):
        Review(carried={finding: Carried.WITHDRAWN})
    await world.review(second["sentinel"], Review(carried={finding: Carried.STILL_PRESENT}))
    assert await world.state() is BehaviourState.IMPLEMENTING

    _, third = await world.candidate_under_review("unchanged again")
    await world.review(third["qa"], APPROVE)
    await world.review(
        third["sentinel"],
        Review(carried={finding: Carried.WITHDRAWN}, withdrawal_reasons={finding: "I misread it"}),
    )

    assert await world.cycle_states() == ["changes_requested", "changes_requested", "satisfied"]


async def test_a_task_id_or_a_guess_grants_nothing(world: World) -> None:
    await world.candidate_under_review()
    async with world.database.connection() as connection:
        task = await (
            await connection.execute("SELECT task_id FROM review_tasks LIMIT 1")
        ).fetchone()
    assert task is not None

    for presented in (str(task["task_id"]), "", "sentinel", "x" * 43):
        with pytest.raises(UnknownCapability):
            await submit_review(world.reviews, presented, APPROVE)
        with pytest.raises(UnknownCapability):
            await prepare_review(world.reviews, presented, world.tmp_path / "nowhere")

    assert await world.count("review_submissions") == 0
    assert await world.count("review_checkouts") == 0


async def test_a_review_of_a_tree_relay_did_not_prepare_is_refused(world: World) -> None:
    _, capabilities = await world.candidate_under_review()

    with pytest.raises(SubmissionRefused, match="no tree was prepared"):
        await submit_review(world.reviews, capabilities["qa"], APPROVE)

    assert await world.count("review_submissions") == 0


async def test_no_capability_leaves_the_control_plane_before_the_candidate_is_published(
    world: World,
) -> None:
    candidate = await world.green_candidate()

    assert await world.capabilities(candidate) == {}
    async with world.database.connection() as connection:
        rows = await (await connection.execute("SELECT capability FROM review_tasks")).fetchall()
    assert [row["capability"] for row in rows] == [None, None]


async def test_a_crash_after_git_accepts_the_ref_announces_each_task_once_on_recovery(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    ref = await candidate_ref_of(world)
    await Mirror(world.mirrors / f"{PROJECT}.git").publish(str(world.repository), candidate, ref)
    assert await world.capabilities(candidate) == {}, "nothing delivered before recovery"

    assert await world.reviews.publish_candidates() == 1
    assert await world.reviews.publish_candidates() == 0

    assert set(await world.capabilities(candidate)) == {"qa", "sentinel"}
    assert await world.count("review_tasks") == 2
    assert world.remote_ref(ref) == candidate


async def test_a_candidate_ref_already_at_another_commit_blocks_the_cycle(world: World) -> None:
    candidate = await world.green_candidate()
    ref = await candidate_ref_of(world)
    world.move_remote_ref(ref, world.red_revision)

    assert await world.reviews.publish_candidates() == 0

    assert await world.capabilities(candidate) == {}
    assert await world.cycle_states() == ["blocked"]
    assert await world.state() is BehaviourState.BLOCKED
    assert world.remote_ref(ref) == world.red_revision
    assert (await world.events())[-1] == "review.cycle_blocked"


async def test_a_reviewer_is_never_given_a_tree_the_other_did_not_see(world: World) -> None:
    _, capabilities = await world.candidate_under_review()
    await prepare_review(world.reviews, capabilities["qa"], world.tmp_path / "qa-tree")
    world.move_remote_ref(await candidate_ref_of(world), world.red_revision)

    with pytest.raises(SubmissionRefused, match="no longer names"):
        await prepare_review(
            world.reviews, capabilities["sentinel"], world.tmp_path / "sentinel-tree"
        )
    late = await submit_review(world.reviews, capabilities["qa"], APPROVE)
    with pytest.raises(SubmissionRefused):
        await prepare_review(world.reviews, capabilities["sentinel"], world.tmp_path / "again")

    assert not (world.tmp_path / "sentinel-tree").exists()
    assert late.disposition == "stale"
    assert await world.cycle_states() == ["blocked"]
    assert await world.state() is BehaviourState.BLOCKED
    assert await world.count("review_checkouts") == 1


async def test_a_blocker_whose_reviewer_is_no_longer_required_parks_the_behaviour(
    world: World,
) -> None:
    _, first = await world.candidate_under_review()
    await world.review(first["qa"], APPROVE)
    [finding] = (await world.review(first["sentinel"], Review(findings=(INJECTION,)))).finding_ids
    async with world.database.connection() as connection:
        await connection.execute("UPDATE projects SET required_reviewers = '{qa}'")
    _, second = await world.candidate_under_review("fixed")
    builds = len(await pending_messages(world.database))

    assert set(second) == {"qa"}
    await world.review(second["qa"], APPROVE)

    assert await world.state() is BehaviourState.BLOCKED
    assert await world.cycle_states() == ["changes_requested", "parked"]
    parked = (await read_events(world.database, PROJECT))[-1]
    assert (parked.event_type, parked.payload["blocking"]) == ("review.parked", [finding])
    assert "Product Owner" in parked.payload["reason"]
    assert len(await pending_messages(world.database)) == builds, (
        "no remediation the Builder cannot do"
    )


async def test_the_policy_in_force_when_a_cycle_opened_is_the_one_it_keeps(world: World) -> None:
    _, capabilities = await world.candidate_under_review()
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE projects SET required_reviewers = '{qa}', blocking_from = 1"
        )

    await world.review(capabilities["qa"], Review(findings=(STYLE,)))

    assert await world.cycle_states() == ["open"], "sentinel is still required for this cycle"
    await world.review(capabilities["sentinel"], APPROVE)
    assert await world.cycle_states() == ["satisfied"], "a low finding did not block this cycle"


async def test_a_review_left_without_a_cycle_is_adopted_once(world: World) -> None:
    """A behaviour that reached review before cycles existed has only a recorded commit."""
    candidate = await world.green_candidate()
    async with world.database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    shutil.rmtree(world.mirrors)

    assert await world.reviews.adopt_reviews_without_a_cycle() == 1
    assert await world.reviews.adopt_reviews_without_a_cycle() == 0
    assert await world.reviews.publish_candidates() == 1

    assert await world.cycle_states() == ["open"]
    assert set(await world.capabilities(candidate)) == {"qa", "sentinel"}
    assert world.remote_ref(await candidate_ref_of(world)) == candidate


async def test_a_review_whose_commit_can_no_longer_be_obtained_is_blocked_visibly(
    world: World,
) -> None:
    await world.green_candidate()
    async with world.database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    shutil.rmtree(world.mirrors)
    publish_attempt(
        world.repository, "B1", 1, {"vat.py": "print('moved')\n"}, on=world.red_revision
    )

    assert await world.reviews.adopt_reviews_without_a_cycle() == 0
    assert await world.reviews.adopt_reviews_without_a_cycle() == 0

    assert await world.state() is BehaviourState.BLOCKED
    assert await world.count("review_cycles") == 0
    assert (await world.events()).count("review.recovery_blocked") == 1


async def test_a_task_announced_again_is_the_same_message_and_takes_effect_once(
    world: World, broker: redis.Redis, namespace: str
) -> None:
    """Through Redis and the consumer's receipt, as a real reviewer worker would see it."""
    _, capabilities = await world.candidate_under_review()
    await world.review(capabilities["qa"], APPROVE)
    streams = Streams(broker, prefix=namespace)
    stream = f"relay:{PROJECT}:cmd:sentinel"
    async with world.database.connection() as connection:
        await connection.execute("CREATE TABLE reviews_started (message_id uuid NOT NULL)")

    async def sentinel_worker() -> list[dict[str, Any]]:
        deliveries = await streams.read(stream, group="sentinel", consumer="worker-1")
        for delivery in deliveries:
            async with world.database.connection() as connection, connection.transaction():
                if await act_once(connection, "sentinel", delivery.message_id):
                    await connection.execute(
                        "INSERT INTO reviews_started VALUES (%s)", (delivery.message_id,)
                    )
            await streams.acknowledge(stream, "sentinel", delivery)
        return [delivery.envelope for delivery in deliveries]

    await OutboxPublisher(world.database, streams).publish_pending()
    [first] = await sentinel_worker()
    assert await announce_wanted_work_again(world.database, PROJECT) == 1, (
        "only sentinel still owes a review"
    )
    await OutboxPublisher(world.database, streams).publish_pending()
    [again] = await sentinel_worker()

    assert again == first
    assert await world.count("reviews_started") == 1


async def test_two_publishers_at_once_announce_each_task_once(world: World) -> None:
    candidate = await world.green_candidate()
    second_control_plane = Reviews(world.database, world.mirrors)

    published = await asyncio.gather(
        world.reviews.publish_candidates(), second_control_plane.publish_candidates()
    )

    assert sum(published) == 1
    commands = [
        message.envelope["recipient"]
        for message in await pending_messages(world.database)
        if message.envelope["event_type"] == "review.requested"
    ]
    assert sorted(commands) == ["qa", "sentinel"]
    assert set(await world.capabilities(candidate)) == {"qa", "sentinel"}
    assert (await world.events()).count("review.candidate_published") == 1
    assert await world.cycle_states() == ["open"]
    assert await world.state() is BehaviourState.REVIEW


async def test_two_recoveries_at_once_adopt_a_review_once(world: World) -> None:
    await world.green_candidate()
    async with world.database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    second_control_plane = Reviews(world.database, world.mirrors)

    adopted = await asyncio.gather(
        world.reviews.adopt_reviews_without_a_cycle(),
        second_control_plane.adopt_reviews_without_a_cycle(),
    )

    assert sum(adopted) == 1
    assert await world.cycle_states() == ["open"]
    assert (await world.events()).count("review.cycle_adopted") == 1


async def cycle_row(world: World) -> dict[str, Any]:
    async with world.database.connection() as connection:
        row = await (
            await connection.execute("SELECT * FROM review_cycles ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
    assert row is not None
    return row


async def test_a_review_with_several_findings_gives_each_its_own_lasting_id(world: World) -> None:
    _, capabilities = await world.candidate_under_review()
    several = Review(findings=(INJECTION, STYLE, INJECTION, STYLE, INJECTION))

    first = await world.review(capabilities["sentinel"], several)
    replay = await submit_review(world.reviews, capabilities["sentinel"], several)

    assert len(set(first.finding_ids)) == 5
    assert replay.finding_ids == first.finding_ids
    assert await world.count("findings") == 5


async def test_retrying_the_approval_that_satisfied_the_gate_says_the_cycle_is_still_current(
    world: World,
) -> None:
    _, capabilities = await world.candidate_under_review()
    await world.review(capabilities["qa"], APPROVE)
    last = await world.review(capabilities["sentinel"], APPROVE)

    replay = await submit_review(world.reviews, capabilities["sentinel"], APPROVE)

    assert (replay.submission_id, replay.disposition) == (last.submission_id, "effective")
    assert replay.cycle_is_current is True
    assert (await world.events()).count("review.gate_satisfied") == 1


async def test_adoption_binds_the_green_the_ledger_accepted_not_the_latest_run_of_that_commit(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    accepted = (await cycle_row(world))["green_execution_id"]
    async with world.database.connection() as connection:
        await connection.execute(
            "INSERT INTO executions (execution_id, project_id, behaviour_id, purpose, revision, "
            "contract_digest, isolation, runtime_identity, exit_code, stdout, stderr, recorded_at) "
            "SELECT gen_random_uuid(), project_id, behaviour_id, purpose, revision, contract_digest, "
            "isolation, runtime_identity, exit_code, stdout, stderr, now() + interval '1 hour' "
            "FROM executions WHERE execution_id = %s",
            (accepted,),
        )
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
        # Rewrite the accepted row so that, on disk, the other run comes first: a query
        # that merely takes "a green run of this commit" then finds the wrong one.
        await connection.execute(
            "UPDATE executions SET stdout = stdout WHERE execution_id = %s", (accepted,)
        )

    assert await world.reviews.adopt_reviews_without_a_cycle() == 1

    adopted = await cycle_row(world)
    assert adopted["green_execution_id"] == accepted
    assert adopted["candidate_revision"] == candidate


async def test_a_lost_mirror_is_rebuilt_from_the_published_candidate(world: World) -> None:
    candidate, capabilities = await world.candidate_under_review()
    shutil.rmtree(world.mirrors)
    publish_attempt(
        world.repository, "B1", 1, {"vat.py": "print('moved')\n"}, on=world.red_revision
    )

    tree = await prepare_review(world.reviews, capabilities["qa"], world.tmp_path / "qa-tree")

    assert (tree / "vat.py").read_text().startswith(VAT_APPLIES_STANDARD_RATE)
    assert await Mirror(world.mirrors / f"{PROJECT}.git").holds(candidate)
    assert await world.cycle_states() == ["open"]


async def test_a_legacy_review_is_adopted_from_an_attempt_that_has_since_moved_on(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    async with world.database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    shutil.rmtree(world.mirrors)
    publish_attempt(world.repository, "B1", 1, {"NOTES.md": "more work on top"}, on=candidate)

    assert await world.reviews.adopt_reviews_without_a_cycle() == 1
    assert await world.reviews.publish_candidates() == 1

    assert await Mirror(world.mirrors / f"{PROJECT}.git").holds(candidate)
    assert world.remote_ref(await candidate_ref_of(world)) == candidate
    assert await world.state() is BehaviourState.REVIEW


async def test_a_remote_that_cannot_be_reached_defers_publication_and_does_not_block(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await world.green_candidate()
    real = reviews_module.refs_under
    real_resolve = reviews_module.resolve_ref

    async def unreachable(*_: object, **__: object) -> None:
        raise RemoteUnavailable("git ls-remote failed: could not connect")

    monkeypatch.setattr(reviews_module, "resolve_ref", unreachable)
    monkeypatch.setattr(reviews_module, "refs_under", unreachable)
    assert await world.reviews.publish_candidates() == 0
    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert await world.state() is BehaviourState.REVIEW
    assert (await cycle_row(world))["deferred_reason"] == "git ls-remote failed: could not connect"
    assert (await world.events()).count("review.publication_deferred") == 1
    assert await world.capabilities(candidate) == {}

    monkeypatch.setattr(reviews_module, "resolve_ref", real_resolve)
    monkeypatch.setattr(reviews_module, "refs_under", real)
    assert await world.reviews.publish_candidates() == 1

    assert set(await world.capabilities(candidate)) == {"qa", "sentinel"}
    assert (await cycle_row(world))["deferred_reason"] is None


async def test_a_remote_that_refuses_the_candidate_blocks_with_the_remotes_reason(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    hook = world.repository / ".git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'candidates are not accepted here' >&2\nexit 1\n")
    hook.chmod(0o755)

    assert await world.reviews.publish_candidates() == 0

    blocked = await cycle_row(world)
    assert blocked["state"] == "blocked"
    assert "hook declined" in blocked["blocked_reason"]
    assert await world.state() is BehaviourState.BLOCKED
    assert await world.capabilities(candidate) == {}


async def test_a_conflict_seen_while_the_last_review_lands_leaves_one_decision_and_both_on_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel has approved; QA has its tree. The candidate ref moves. QA's worker asks
    for the tree again and sees the conflict, but before it can act QA's final approval
    satisfies the gate. Both things happened; the cycle must end blocked, with both on
    the ledger."""
    _, capabilities = await world.candidate_under_review()
    await world.review(capabilities["sentinel"], APPROVE)
    await prepare_review(world.reviews, capabilities["qa"], world.tmp_path / "qa-tree")
    world.move_remote_ref(await candidate_ref_of(world), world.red_revision)
    seen, release = asyncio.Event(), asyncio.Event()
    real_resolve = reviews_module.resolve_ref

    async def held_after_looking(repository: str, ref: str, **options: Any) -> str | None:
        found = await real_resolve(repository, ref, **options)
        seen.set()
        await release.wait()
        return found

    monkeypatch.setattr(reviews_module, "resolve_ref", held_after_looking)
    preparing = asyncio.create_task(
        prepare_review(world.reviews, capabilities["qa"], world.tmp_path / "again")
    )
    await asyncio.wait_for(seen.wait(), 30)

    final = await submit_review(world.reviews, capabilities["qa"], APPROVE)
    release.set()
    with pytest.raises(SubmissionRefused, match="no longer names"):
        await preparing

    assert final.disposition == "effective"
    assert await world.cycle_states() == ["blocked"]
    assert await world.state() is BehaviourState.BLOCKED
    events = await world.events()
    assert events.index("review.gate_satisfied") < events.index("review.cycle_blocked")


async def test_a_fetch_cut_short_does_not_declare_an_available_commit_lost(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await world.green_candidate()
    shutil.rmtree(world.mirrors)
    real_result = mirror_module.git_result

    async def connection_drops_during_fetch(*arguments: str, **options: Any) -> Any:
        if "fetch" in arguments:
            return 128, b"", "fatal: the remote end hung up unexpectedly"
        return await real_result(*arguments, **options)

    monkeypatch.setattr(mirror_module, "git_result", connection_drops_during_fetch)
    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert await world.state() is BehaviourState.REVIEW
    assert "hung up" in (await cycle_row(world))["deferred_reason"]

    monkeypatch.setattr(mirror_module, "git_result", real_result)
    assert await world.reviews.publish_candidates() == 1

    assert world.remote_ref(await candidate_ref_of(world)) == candidate
    assert set(await world.capabilities(candidate)) == {"qa", "sentinel"}


async def test_a_push_that_never_reached_the_remote_leaves_the_cycle_pending(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await world.green_candidate()
    real_publish = Mirror.publish

    async def cannot_send(self: Mirror, *_: object) -> None:
        raise RemoteUnavailable("git push failed: Permission denied (publickey)")

    monkeypatch.setattr(Mirror, "publish", cannot_send)
    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert "Permission denied" in (await cycle_row(world))["deferred_reason"]

    monkeypatch.setattr(Mirror, "publish", real_publish)
    assert await world.reviews.publish_candidates() == 1
    assert world.remote_ref(await candidate_ref_of(world)) == candidate


async def test_one_projects_unreachable_repository_does_not_hold_up_another(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    first = World(database, tmp_path / "first", runner_image)
    await first.release_to_builder()
    await first.green_candidate()
    second = World(database, tmp_path / "second", runner_image)
    second.reviews = first.reviews
    second.behaviours = first.behaviours
    second_trunk = commit(second.repository, {"vat.py": VAT_RETURNS_NET})
    await first.behaviours.open_project("other-project", str(second.repository))
    await in_a_story(first.behaviours, "other-project")
    await first.behaviours.propose(
        "other-project",
        "S1",
        "B1",
        "standard VAT rate is applied",
        ["AC1"],
        basis=0,
        story_revision=1,
    )
    await first.behaviours.begin_test_design("other-project", "B1")
    await first.behaviours.register_acceptance_test("other-project", "B1", CONTRACT, basis=0)
    assert (await first.behaviours.run_red("other-project", "B1")).is_valid
    publish_attempt(
        second.repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE},
        on=second_trunk, project="other-project",
    )  # fmt: skip
    assert (await run_green(first.behaviours, "other-project", "B1", attempt=1)).is_valid
    shutil.rmtree(first.mirrors / f"{PROJECT}.git")
    first.repository.rename(tmp_path / "first" / "unplugged")

    assert await first.reviews.publish_candidates() == 1

    async with database.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT project_id, publication, state, deferred_reason FROM review_cycles "
                "ORDER BY created_at"
            )
        ).fetchall()
    assert [(row["project_id"], row["publication"], row["state"]) for row in rows] == [
        (PROJECT, "pending", "open"),
        ("other-project", "published", "open"),
    ]
    assert rows[0]["deferred_reason"] is not None
    assert await first.state() is BehaviourState.REVIEW


async def test_an_unforeseen_git_failure_defers_the_cycle_instead_of_stopping_the_pass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.green_candidate()

    async def breaks(self: Reviews, record: Any) -> bool:
        raise CheckoutFailed("git fetch failed: something nobody classified")

    monkeypatch.setattr(Reviews, "_recover", breaks)

    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert "nobody classified" in (await cycle_row(world))["deferred_reason"]


async def test_a_pending_candidate_that_is_truly_gone_is_blocked_once_not_left_pending(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    shutil.rmtree(world.mirrors)
    publish_attempt(
        world.repository, "B1", 1, {"vat.py": "print('moved')\n"}, on=world.red_revision
    )

    assert await world.reviews.publish_candidates() == 0
    assert await world.reviews.publish_candidates() == 0

    blocked = await cycle_row(world)
    assert (blocked["state"], blocked["publication"]) == ("blocked", "pending")
    assert "can no longer be obtained" in blocked["blocked_reason"]
    assert await world.state() is BehaviourState.BLOCKED
    assert (await world.events()).count("review.cycle_blocked") == 1
    assert await world.capabilities(candidate) == {}


async def test_an_ancestry_check_that_fails_defers_instead_of_declaring_the_commit_lost(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await world.green_candidate()
    shutil.rmtree(world.mirrors)
    real = mirror_module.git_answers_yes

    async def object_store_error(*arguments: str) -> bool:
        if "merge-base" in arguments:
            raise CheckoutFailed("git merge-base failed: fatal: unable to read object")
        return await real(*arguments)

    monkeypatch.setattr(mirror_module, "git_answers_yes", object_store_error)
    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert "unable to read object" in (await cycle_row(world))["deferred_reason"]

    monkeypatch.setattr(mirror_module, "git_answers_yes", real)
    assert await world.reviews.publish_candidates() == 1
    assert world.remote_ref(await candidate_ref_of(world)) == candidate


async def test_a_push_of_unknown_outcome_that_left_no_ref_is_retried_not_blocked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await world.green_candidate()
    real_result = mirror_module.git_result

    async def remote_did_not_report(*arguments: str, **options: Any) -> tuple[int, bytes, str]:
        if "push" in arguments:
            destination = arguments[-1]
            status = f"!\t{destination}\t[remote failure] (remote failed to report status)\n"
            return 1, status.encode(), "error: failed to push some refs"
        return await real_result(*arguments, **options)

    monkeypatch.setattr(mirror_module, "git_result", remote_did_not_report)
    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert "remote failure" in (await cycle_row(world))["deferred_reason"]
    assert await world.capabilities(candidate) == {}

    monkeypatch.setattr(mirror_module, "git_result", real_result)
    assert await world.reviews.publish_candidates() == 1
    assert sorted(await world.capabilities(candidate)) == ["qa", "sentinel"]


async def test_a_push_reported_as_already_there_while_the_ref_is_absent_is_retried_not_blocked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.green_candidate()

    async def claims_the_ref_exists(self: Mirror, *_: object) -> None:
        raise CheckoutFailed("the push was not accepted: [rejected] (stale info)")

    monkeypatch.setattr(Mirror, "publish", claims_the_ref_exists)

    assert await world.reviews.publish_candidates() == 0

    assert await world.cycle_states() == ["open"]
    assert "still absent" in (await cycle_row(world))["deferred_reason"]


async def test_a_mirror_directory_left_empty_by_an_interrupted_start_does_not_stop_recovery(
    world: World,
) -> None:
    candidate = await world.green_candidate()
    shutil.rmtree(world.mirrors)
    (world.mirrors / f"{PROJECT}.git").mkdir(parents=True)

    assert await world.reviews.publish_candidates() == 1

    assert world.remote_ref(await candidate_ref_of(world)) == candidate
    assert sorted(await world.capabilities(candidate)) == ["qa", "sentinel"]


async def test_one_unreadable_legacy_mirror_does_not_stop_adoption_for_another_project(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = World(database, tmp_path / "first", runner_image)
    await first.release_to_builder()
    await first.green_candidate()
    other_repository = tmp_path / "second" / "subject"
    other_trunk = commit(other_repository, {"vat.py": VAT_RETURNS_NET})
    await first.behaviours.open_project("other-project", str(other_repository))
    await in_a_story(first.behaviours, "other-project")
    await first.behaviours.propose(
        "other-project",
        "S1",
        "B1",
        "standard VAT rate is applied",
        ["AC1"],
        basis=0,
        story_revision=1,
    )
    await first.behaviours.begin_test_design("other-project", "B1")
    await first.behaviours.register_acceptance_test("other-project", "B1", CONTRACT, basis=0)
    assert (await first.behaviours.run_red("other-project", "B1")).is_valid
    publish_attempt(
        other_repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE},
        on=other_trunk, project="other-project",
    )  # fmt: skip
    assert (await run_green(first.behaviours, "other-project", "B1", attempt=1)).is_valid
    async with database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    shutil.rmtree(first.mirrors)
    real = mirror_module.git_answers_yes

    async def first_projects_objects_unreadable(*arguments: str) -> bool:
        if "merge-base" in arguments and f"{PROJECT}.git" in arguments[1]:
            raise CheckoutFailed("git merge-base failed: fatal: unable to read object")
        return await real(*arguments)

    monkeypatch.setattr(mirror_module, "git_answers_yes", first_projects_objects_unreadable)
    assert await first.reviews.adopt_reviews_without_a_cycle() == 1
    assert await first.reviews.adopt_reviews_without_a_cycle() == 0

    async def cycles() -> list[str]:
        async with database.connection() as connection:
            rows = await (
                await connection.execute("SELECT project_id FROM review_cycles ORDER BY project_id")
            ).fetchall()
        return [row["project_id"] for row in rows]

    assert await cycles() == ["other-project"]
    assert await first.state() is BehaviourState.REVIEW
    assert (await first.events()).count("review.adoption_deferred") == 1

    monkeypatch.setattr(mirror_module, "git_answers_yes", real)
    assert await first.reviews.adopt_reviews_without_a_cycle() == 1
    assert await first.reviews.adopt_reviews_without_a_cycle() == 0
    assert await cycles() == ["other-project", PROJECT]


async def test_a_deferral_that_arrives_after_the_review_was_adopted_is_not_recorded(
    world: World,
) -> None:
    await world.green_candidate()
    before = await world.events()

    await world.reviews._adoption_deferred(PROJECT, "B1", "git fetch failed: a slow failure")

    assert await world.events() == before


async def test_two_reconcilers_failing_alike_record_one_deferral(world: World) -> None:
    await world.green_candidate()
    async with world.database.connection() as connection:
        await connection.execute("DELETE FROM review_tasks")
        await connection.execute("DELETE FROM review_cycles")
    other = Reviews(world.database, world.mirrors)

    await asyncio.gather(
        world.reviews._adoption_deferred(PROJECT, "B1", "git fetch failed: no route"),
        other._adoption_deferred(PROJECT, "B1", "git fetch failed: no route"),
    )

    assert (await world.events()).count("review.adoption_deferred") == 1
