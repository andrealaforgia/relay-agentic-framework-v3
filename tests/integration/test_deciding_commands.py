"""The Product Owner's commands, run as a person runs them: from a shell in the project's
checkout, at a terminal, with the passphrase. And the reading commands, which anyone may
run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.application.leadership import installation_of
from relay.cli import journal
from relay.domain.story import StoryState
from relay.git.checkout import name_attempts_under
from relay.runtime import channel, configure
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import ANALYST
from tests.integration.person import Ran, at_a_terminal, with_no_terminal
from tests.integration.story_world import STANDARD_RATE
from tests.integration.trunk_world import PROJECT

PASSPHRASE = "correct horse battery"


class Desk:
    """A Product Owner at a terminal in the project's checkout, and the world behind."""

    def __init__(self, world: DecisionWorld, shell: dict[str, str]) -> None:
        self.world, self.shell, self.here = world, shell, world.builder

    def po(self, *command: str, typing: tuple[str, ...] = (PASSPHRASE,)) -> Ran:
        return at_a_terminal(list(command), list(typing), self.shell, self.here)

    def anyone(self, *command: str) -> Ran:
        return with_no_terminal(list(command), self.shell, self.here)

    def read(self, *command: str) -> Any:
        ran = self.anyone(*command, "--json")
        assert ran.code == 0, ran.printed
        return json.loads(ran.out)

    async def proposed(self) -> str:
        """The Analyst's proposal for F1, as a worker would publish it."""
        gate = await self.world.stories.publish_roadmap(
            ANALYST, PROJECT, "F1", [STANDARD_RATE], basis=0
        )
        return str(gate)


@pytest.fixture
async def desk(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path, runner_image: str
) -> Any:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await configure(
        database,
        workspaces=str(tmp_path.resolve() / "workspaces"),
        mirrors=str(world.mirrors),
        image=runner_image,
    )
    await authority.set_first(database, PASSPHRASE)
    yield Desk(world, shell)
    name_attempts_under("")


async def test_a_feature_is_started_its_roadmap_read_and_approved_from_the_command_line(
    desk: Desk,
) -> None:
    world = desk.world
    async with world.database.connection() as connection:
        installation = await installation_of(connection)
    listening = await world.database.getconn()
    await listening.set_autocommit(True)
    await listening.execute(f"LISTEN {channel(installation)}")

    started = desk.po("feature", "start", "prices need VAT")

    assert started.code == 0, started.printed
    assert "effective: started F1" in started.printed
    assert "prices need VAT" in started.shown  # shown where the passphrase was then read
    assert started.shown.index("prices need VAT") < started.shown.index("passphrase:")
    assert PASSPHRASE not in started.printed + started.shown
    gate = await desk.proposed()

    [item] = desk.read("inbox")
    assert (item["item_id"], item["kind"]) == (gate, "gate")
    assert item["actions"] == ["approve", "request-changes"]
    assert gate[:8] in desk.anyone("inbox").out
    shown = desk.read("show", gate[:8])
    assert shown == item and "the roadmap of F1" in desk.anyone("show", gate[:8]).out
    assert desk.read("roadmap")["proposal_waiting"] == gate
    assert desk.read("status")["waiting_for_the_product_owner"] == 1

    approved = desk.po("approve", gate[:8])

    assert approved.code == 0, approved.printed
    assert "effective:" in approved.printed
    assert "the roadmap of F1" in approved.shown and item["basis"] in approved.shown
    assert await world.story("S1") is StoryState.RELEASED
    assert desk.read("inbox") == []
    facts = desk.read("status")
    [feature] = facts["features"]
    assert feature["feature"] == "F1" and feature["stories"][0]["story"] == "S1"
    assert facts["waiting_for_the_product_owner"] == 0
    assert "S1" in desk.anyone("status").out and "S1" in desk.anyone("roadmap").out
    events = desk.read("ledger")
    assert [each["event"] for each in events][:2] == ["project.opened", "feature.opened"]
    assert desk.read("ledger", "--since", str(events[-1]["sequence"])) == []
    # Each decision told the control plane there may be something for it to do.
    # A wake is a hint and may be sent more than once for one decision: at least one each.
    woken = [notice.payload async for notice in listening.notifies(timeout=2)]
    assert len(woken) >= 2 and set(woken) == {PROJECT}
    await listening.execute("UNLISTEN *")
    await listening.set_autocommit(False)
    await world.database.putconn(listening)

    # 3C-1.7: the handle printed first repeats the request, and repeats nothing else.
    handle = approved.printed.split()[1]
    again = desk.anyone("approve", gate[:8], "--request-id", handle)
    assert again.code == 0 and "as on record" in again.printed and "effective" in again.printed
    noted = journal.read(Path(desk.shell["RELAY_STATE_DIR"]) / "requests.jsonl")
    assert [(each.command[0], each.project) for each in noted] == [
        ("feature", PROJECT),
        ("approve", PROJECT),
    ]


async def test_what_a_model_can_run_reads_everything_and_decides_nothing(desk: Desk) -> None:
    """S3-1.8: with no controlling terminal, or without the passphrase, no deciding
    command takes effect, whatever is on its standard input or in its environment."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    as_a_model = {**desk.shell, "RELAY_PASSPHRASE": PASSPHRASE}

    for command in (
        ["approve", str(gate)],
        ["request-changes", str(gate), "--reason", "not this"],
        ["pause", "F1", "--reason", "because"],
        ["cancel", "F1", "--reason", "because"],
        ["feature", "start", "something of the model's own"],
    ):
        ran = with_no_terminal(command, as_a_model, desk.here, given=PASSPHRASE + "\n")
        assert ran.code == 1 and "needs a person at a terminal" in ran.printed, command
    guessed = desk.po("approve", str(gate), typing=("a guess at it",))

    assert guessed.code == 1 and "not the passphrase" in guessed.printed
    assert await world.column("SELECT state FROM gates") == ["open"]
    assert await world.column("SELECT count(*) FROM features") == [1]
    assert await world.column("SELECT closed_by FROM features") == [None]
    assert len(desk.read("inbox")) == 1  # and it reads what it likes


async def test_each_kind_of_thing_is_decided_by_its_own_command_and_refuses_the_others(
    desk: Desk,
) -> None:
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)

    # A gate is not answered, and a reason is asked for where one is owed.
    assert "is not something to answer" in desk.po("answer", str(gate), "yes").printed
    assert desk.po("retry", str(gate)).code == 1
    assert desk.po("approve", "nonesuch").code == 1
    back = desk.po("request-changes", str(gate), "--reason", "split the story in two")
    assert back.code == 0 and "split the story in two" in back.shown
    assert await world.column("SELECT outcome FROM gates") == ["changes_requested"]

    again = await world.revise(STANDARD_RATE)
    assert desk.po("approve", str(again)).code == 0
    question = await world.ask()

    answered = desk.po("answer", str(question)[:8], "the standard rate, always")

    assert answered.code == 0, answered.printed
    assert "the standard rate, always" in answered.shown
    assert await world.column("SELECT text FROM decisions") == ["the standard rate, always"]
    [decision] = await world.column("SELECT decision_id FROM decisions")
    replaced = desk.po("replace-decision", "F1/D1", "the reduced rate for books")
    assert replaced.code == 0, replaced.printed
    assert "the standard rate, always" in replaced.shown and "books" in replaced.shown
    assert await world.column("SELECT count(*) FROM decisions") == [2]
    assert decision is not None


async def test_a_feature_is_paused_resumed_and_cancelled_and_a_resume_lifts_the_pause_it_showed(
    desk: Desk,
) -> None:
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))

    assert desk.po("resume", "F1").code == 1  # not paused: nothing to resume
    paused = desk.po("pause", "F1", "--reason", "waiting on legal")
    assert paused.code == 0 and "effective: paused" in paused.printed
    assert desk.read("status")["features"][0]["state"] == "paused"

    resumed = desk.po("resume", "F1")

    assert resumed.code == 0 and "effective: resumed" in resumed.printed
    assert "the pause this lifts is entry" in resumed.shown
    assert await world.column("SELECT closed_by FROM features") == [None]
    cancelled = desk.po("cancel", "F1", "--reason", "no longer wanted")
    assert cancelled.code == 0 and "effective: cancelled" in cancelled.printed
    assert desk.po("pause", "F1", "--reason", "too late").code == 1  # stale: cancelled for good


async def test_a_request_sent_again_from_elsewhere_means_the_project_it_was_made_for(
    desk: Desk, tmp_path: Path
) -> None:
    """3C-2.6: the journal records the project a request was resolved to. Sent again from
    a directory that is no checkout at all, it is that project's request still."""
    gate = await desk.world.roadmap(STANDARD_RATE)
    approved = desk.po("approve", str(gate))
    handle = approved.printed.split()[1]

    elsewhere = with_no_terminal(
        ["approve", str(gate), "--request-id", handle], desk.shell, tmp_path
    )
    unsaid = with_no_terminal(["inbox"], desk.shell, tmp_path)

    assert elsewhere.code == 0 and "as on record" in elsewhere.printed
    assert unsaid.code == 1 and "which project is meant was not said" in unsaid.printed


def code_of(ran: Ran) -> str:
    assert ran.code == 0, ran.printed
    return ran.out.split()[1]


async def test_a_model_prepares_and_only_the_person_confirms(desk: Desk) -> None:
    """PO-3 and S3-1.8: preparing needs no terminal and decides nothing. Confirming shows
    what was prepared, and why, where the passphrase is then read, and is the Product
    Owner's. The code is the handle to what it did, for good."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)

    prepared = desk.anyone("prepare", "approve", str(gate), "--why", "it matches the brief")

    code = code_of(prepared)
    assert "Nothing has been decided" in prepared.out and f"relay confirm {code}" in prepared.out
    assert await world.column("SELECT state FROM gates") == ["open"]
    # A model cannot confirm what it prepared, and a guess at the passphrase confirms nothing.
    as_a_model = with_no_terminal(["confirm", code], desk.shell, desk.here, given=PASSPHRASE + "\n")
    assert as_a_model.code == 1 and "needs a person at a terminal" in as_a_model.printed
    assert desk.po("confirm", code, typing=("a guess at it",)).code == 1
    assert await world.column("SELECT state FROM gates") == ["open"]

    confirmed = desk.po("confirm", code)

    assert confirmed.code == 0, confirmed.printed
    assert "effective:" in confirmed.printed
    assert "the roadmap of F1" in confirmed.shown and "it matches the brief" in confirmed.shown
    assert confirmed.shown.index("it matches the brief") < confirmed.shown.index("passphrase:")
    assert await world.story("S1") is StoryState.RELEASED
    again = desk.anyone("confirm", code)
    assert again.code == 0 and "as on record" in again.printed
    assert desk.anyone("confirm", "nonesuch").code == 1


async def test_what_cannot_be_done_is_not_prepared_and_a_reason_is_carried_with_what_is(
    desk: Desk,
) -> None:
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)

    wrong = desk.anyone("prepare", "answer", str(gate), "yes", "--why", "because")
    unsaid = desk.anyone("prepare", "request-changes", str(gate), "--why", "because")
    back = desk.anyone(
        "prepare", "request-changes", str(gate), "--reason", "split it", "--why", "two outcomes"
    )

    assert wrong.code == 1 and "is not something to answer" in wrong.printed
    assert unsaid.code == 1 and "say why with --reason" in unsaid.printed
    confirmed = desk.po("confirm", code_of(back))
    assert confirmed.code == 0 and "split it" in confirmed.shown
    assert await world.column("SELECT outcome FROM gates") == ["changes_requested"]


async def test_a_prepared_action_is_stale_once_what_it_was_prepared_against_is_decided(
    desk: Desk,
) -> None:
    """3C-1.6: prepared against one gate, confirmed after that gate was decided another
    way. Stale, and the code's answer is that for good."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))
    await world.decide(gate, "request_changes", reason="not yet")

    late = desk.po("confirm", code)

    assert late.code == 1 and late.printed.startswith("stale: nothing changed")
    assert "passphrase" not in late.shown  # nothing was put to the person to confirm
    assert await world.column("SELECT outcome FROM gates") == ["changes_requested"]
    assert "Nothing was done under this code" in desk.anyone("confirm", code).printed


async def test_a_prepared_retry_of_a_call_since_set_aside_authorises_no_call(desk: Desk) -> None:
    """3C-3.5: the item was open when the retry was prepared. The person set the call
    aside. The work is parked still; the prepared retry names an item that is closed."""
    from tests.integration.test_inbox import parked_call
    from tests.integration.test_invocations import Calling

    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await parked_call(calling)
        [item] = desk.read("inbox")
        assert item["kind"] == "parked_call"
        code = code_of(desk.anyone("prepare", "retry", item["item_id"], "--why", "worth one more"))
        assert desk.po("set-aside", item["item_id"][:8]).code == 0

        late = desk.po("confirm", code)

        assert late.code == 1 and "stale" in late.printed
        _, parked, _ = await calling.assignment()
        assert parked is not None
        assert await world.column(
            "SELECT count(*) FROM invocations WHERE retry_authorised_by IS NOT NULL"
        ) == [0]
    finally:
        await calling.close()


async def test_a_prepared_resume_lifts_the_pause_it_was_prepared_against_and_no_later_one(
    desk: Desk,
) -> None:
    """3C-2.7."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.close("pause", reason="waiting on legal")
    code = code_of(desk.anyone("prepare", "resume", "F1", "--why", "legal have answered"))
    await world.close("resume")
    await world.close("pause", reason="waiting on finance")

    late = desk.po("confirm", code)

    assert late.code == 1 and "stale" in late.printed
    assert await world.column("SELECT closed_by FROM features") == ["paused"]


async def test_a_code_expires_unused_and_is_for_the_project_it_was_prepared_for(
    desk: Desk, tmp_path: Path
) -> None:
    """3C-2.6 and the hour a code is good for."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    await world.behaviours.open_project("another", str(world.remote), "next")
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))

    elsewhere = desk.po("confirm", code, "--project", "another")

    assert elsewhere.code == 1 and f"prepared for {PROJECT}" in elsewhere.printed
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE prepared_actions SET expires_at = now() - interval '1 minute'"
        )
    expired = desk.po("confirm", code)
    assert expired.code == 1 and "expired unused" in expired.printed
    assert "passphrase" not in expired.shown
    assert "expired unused" in desk.anyone("confirm", code).printed
    assert await world.column("SELECT state FROM gates") == ["open"]


async def test_a_refused_push_is_retried_with_a_reason_and_its_request_repeated_by_its_handle(
    desk: Desk,
) -> None:
    """3C-1.6 and 3C-1.7: the retry answers the refusal shown, says why, and commits its
    receipt with what it allows. Sent again, the request is given that receipt, though
    the push is by then no longer waiting."""
    from relay.application.integration import Outcome

    world = desk.world
    # The checkout is left on no branch by the Builder's work: the person says which
    # project they mean, since Relay will not guess.
    desk.shell["RELAY_PROJECT"] = PROJECT
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    world.hook_flag("refuse").touch()
    assert (await world.integrate("B1")).outcome is Outcome.REJECTED
    [push] = desk.read("inbox")
    assert push["kind"] == "refused_push" and push["actions"] == ["retry"]
    world.hook_flag("refuse").unlink()

    unsaid = desk.po("retry", push["item_id"][:8])
    retried = desk.po("retry", push["item_id"][:8], "--reason", "the hook was put right")

    assert unsaid.code == 1 and "say why with --reason" in unsaid.printed
    assert retried.code == 0 and "effective: retry_allowed" in retried.printed
    assert "declined" in retried.shown and "the hook was put right" in retried.shown
    assert desk.read("inbox") == []
    assert await world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'integrate' AND state = 'wanted'"
    ) == [1]
    handle = retried.printed.split()[1]
    again = desk.anyone("retry", push["item_id"][:8], "--request-id", handle)
    assert again.code == 0 and "retry_allowed (as on record)" in again.printed
    assert await world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'integrate' AND state = 'wanted'"
    ) == [1]


async def test_a_story_at_its_gate_or_at_work_is_rejected_saying_why(desk: Desk) -> None:
    from tests.integration.story_world import at_the_story_gate

    world = desk.world
    desk.shell["RELAY_PROJECT"] = PROJECT
    gate, _ = await at_the_story_gate(world)

    [item] = desk.read("inbox")
    assert item["item_id"] == str(gate) and "reject" in item["actions"]
    rejected = desk.po("reject", str(gate)[:8], "--reason", "it does not do what was asked")

    assert rejected.code == 0, rejected.printed
    assert "it does not do what was asked" in rejected.shown
    assert await world.story("S1") is StoryState.REJECTED


async def test_a_story_being_worked_on_is_rejected_as_it_stands_when_the_person_is_shown_it(
    desk: Desk,
) -> None:
    """A story at work is not one fixed thing, as a gate is: the rejection names the
    revision and the decisions it stood under, read when the person is shown it."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    assert await world.story("S1") is StoryState.RELEASED

    unsaid = desk.po("reject", "nonesuch", "--reason", "no such story")
    rejected = desk.po("reject", "S1", "--reason", "the outcome is the wrong one")

    assert unsaid.code == 1 and "there is no story of that name" in unsaid.printed
    assert rejected.code == 0, rejected.printed
    assert "which is being worked on" in rejected.shown and "at revision 1" in rejected.shown
    assert await world.story("S1") is StoryState.REJECTED


def meanwhile(namespace: str, *actions: tuple[str, str]) -> Any:
    """Someone else pauses, resumes or cancels F1 while the person is at the prompt: done
    from a thread of its own, with a connection of its own, as another command would."""
    import asyncio
    import threading
    import uuid

    from relay.application.stories import close_feature
    from relay.domain.story import HumanAction
    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL
    from tests.integration.fixtures import PRODUCT_OWNER

    async def done() -> None:
        async with open_pool(DATABASE_URL, schema=namespace) as pool:
            for action, reason in actions:
                payload = {"reason": reason} if reason else {}
                await close_feature(
                    pool,
                    PROJECT,
                    HumanAction(PRODUCT_OWNER, str(uuid.uuid4()), "F1", action, payload),
                )

    def as_another_command() -> None:
        other = threading.Thread(target=lambda: asyncio.run(done()))
        other.start()
        other.join()

    return as_another_command


async def test_a_decision_is_held_to_what_was_shown_while_the_person_looked_at_it(
    desk: Desk, namespace: str
) -> None:
    """3C-2.7, in the window that matters: the person is shown the feature and takes
    their time over the passphrase. Someone resumes it and pauses it again meanwhile.
    The resume they then confirm names a pause that is over, and lifts nothing. The same
    for a cancel prepared earlier and confirmed after the feature has moved."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.close("pause", reason="waiting on legal")

    resumed = at_a_terminal(
        ["resume", "F1"],
        [PASSPHRASE],
        desk.shell,
        desk.here,
        before_typing=meanwhile(namespace, ("resume", ""), ("pause", "waiting on finance")),
    )

    assert resumed.code == 1 and "stale" in resumed.printed, resumed.printed
    assert await world.column("SELECT closed_by FROM features") == ["paused"]

    code = code_of(desk.anyone("prepare", "cancel", "F1", "--reason", "dropped", "--why", "asked"))
    cancelled = at_a_terminal(
        ["confirm", code],
        [PASSPHRASE],
        desk.shell,
        desk.here,
        before_typing=meanwhile(namespace, ("resume", "")),
    )

    assert cancelled.code == 1 and "stale" in cancelled.printed, cancelled.printed
    assert await world.column("SELECT closed_by, state FROM features") == [None]
