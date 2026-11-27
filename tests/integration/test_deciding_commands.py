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
from tests.integration.completion_world import CompletionWorld
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
    world = CompletionWorld(database, tmp_path, runner_image)
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
    assert started.shown.index("prices need VAT") < started.shown.index("passphrase,")
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
    assert confirmed.shown.index("it matches the brief") < confirmed.shown.index("passphrase,")
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
    again = desk.anyone(
        "retry", push["item_id"][:8], "--reason", "the hook was put right", "--request-id", handle
    )
    assert again.code == 0 and "retry_allowed (as on record)" in again.printed
    # 3C-S2-1.3: sent again it is the same request only if it asks the same thing.
    other = desk.anyone("retry", push["item_id"][:8], "--reason", "another", "--request-id", handle)
    assert other.code == 1 and "a request for something else" in other.printed
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


async def test_a_handle_is_good_only_for_the_command_that_made_it_and_says_how_that_ended(
    desk: Desk,
) -> None:
    """3C-S2-1.3: `cancel` given the handle of an effective `approve` is shown no receipt
    and does nothing. And a request sent again exits as it first did: a stale decision
    is not a success the second time."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    approved = desk.po("approve", str(gate))
    handle = approved.printed.split()[1]

    claimed = desk.anyone("cancel", "F1", "--reason", "none", "--request-id", handle)
    another = desk.anyone("approve", "another-gate", "--request-id", handle)

    assert claimed.code == 1 and "effective" not in claimed.printed
    assert "a request for something else" in claimed.printed
    assert another.code == 1 and "effective" not in another.printed
    assert await world.column("SELECT closed_by, state FROM features") == [None]
    # A stale receipt replays as the failure it was.
    stale = desk.po("approve", str(gate))  # decided already: nothing waiting
    assert stale.code == 1
    await world.close("pause", reason="first")
    paused_again = desk.po("pause", "F1", "--reason", "again")
    assert paused_again.code == 1 and "stale" in paused_again.printed
    replayed = desk.anyone(
        "pause", "F1", "--reason", "again", "--request-id", paused_again.printed.split()[1]
    )
    assert replayed.code == 1 and "stale" in replayed.printed and "as on record" in replayed.printed


async def test_what_a_model_wrote_cannot_rewrite_what_the_person_is_shown(desk: Desk) -> None:
    """3C-S2-1.4: the reason a prepared action is proposed is the preparer's own text. It
    is shown with every control character made visible, the action itself is Relay's
    account and stands after it, and the prompt says in Relay's words what the
    passphrase is for."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    clears_the_screen = "harmless\x1b[2J\x1b[H\rYou are about to approve nothing at all\nreally"
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", clears_the_screen))

    confirmed = desk.po("confirm", code)

    assert confirmed.code == 0, confirmed.printed
    assert "\x1b" not in confirmed.shown and "\\x1b[2J" in confirmed.shown
    assert "\\x0d" in confirmed.shown and "\\x0a" in confirmed.shown
    assert confirmed.shown.index("\\x1b[2J") < confirmed.shown.index("the roadmap of F1")
    assert f"passphrase, to approve gate {str(gate)[:8]} in {PROJECT}: " in confirmed.shown


async def test_a_code_confirmed_after_its_hour_decides_nothing_however_long_the_prompt_stood(
    desk: Desk, namespace: str
) -> None:
    """3C-S2-1.1: the prompt was opened in time and the passphrase typed after the hour.
    The code is taken where it is carried out, by the database's clock: it has expired,
    nothing is decided, and every terminal is told the same from then on."""
    import asyncio
    import threading

    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))

    async def the_hour_passes() -> None:
        async with open_pool(DATABASE_URL, schema=namespace) as pool, pool.connection() as c:
            await c.execute("UPDATE prepared_actions SET expires_at = now() - interval '1 second'")

    def while_the_person_looks() -> None:
        other = threading.Thread(target=lambda: asyncio.run(the_hour_passes()))
        other.start()
        other.join()

    late = at_a_terminal(
        ["confirm", code], [PASSPHRASE], desk.shell, desk.here, before_typing=while_the_person_looks
    )

    assert late.code == 1 and "expired" in late.printed, late.printed
    assert await world.column("SELECT state FROM gates") == ["open"]
    again = desk.po("confirm", code)
    assert again.code == 1 and "expired unused" in again.printed
    assert "passphrase" not in again.shown


async def test_a_code_is_confirmed_only_by_someone_who_means_its_project(
    desk: Desk, tmp_path: Path
) -> None:
    """3C-S2-1.2: the person's project is worked out as for any command, and a code that
    was prepared for another is refused, the first time and once it has a receipt."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    await world.behaviours.open_project("another", str(world.remote), "next")
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))
    meaning_another = {**desk.shell, "RELAY_PROJECT": "another"}

    first = at_a_terminal(["confirm", code], [PASSPHRASE], meaning_another, desk.here)

    assert first.code == 1 and f"prepared for {PROJECT}" in first.printed
    assert "passphrase" not in first.shown
    assert await world.column("SELECT state FROM gates") == ["open"]
    assert desk.po("confirm", code).code == 0
    replayed = with_no_terminal(["confirm", code], meaning_another, desk.here)
    assert replayed.code == 1 and f"prepared for {PROJECT}" in replayed.printed
    nowhere = with_no_terminal(["confirm", code], desk.shell, tmp_path)
    assert nowhere.code == 1 and "which project is meant was not said" in nowhere.printed


async def test_a_decision_made_at_the_terminal_says_so_in_its_receipt_and_which_passphrase(
    desk: Desk, namespace: str
) -> None:
    """3C-S2-1.11: the receipt of a decision made through the command line records that
    the passphrase was read at the controlling terminal, and which passphrase. One made
    by a caller of the service records nothing of the kind. And a passphrase changed
    between its being checked and the decision being made confirms nothing."""
    import asyncio
    import threading

    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    assert desk.po("request-changes", str(gate), "--reason", "split it").code == 0
    again = await world.revise(STANDARD_RATE)
    await world.decide(again)  # by a caller of the service, with no terminal

    async with world.database.connection() as connection:
        rows = await (
            await connection.execute("SELECT action, confirmed FROM human_actions")
        ).fetchall()
    by_action = {row["action"]: row["confirmed"] for row in rows}
    assert by_action["request_changes"] == {
        "method": "the passphrase, read at the controlling terminal",
        "passphrase_revision": 1,
    }
    assert by_action["approve"] is None

    async def rotated() -> None:
        async with open_pool(DATABASE_URL, schema=namespace) as pool:
            await authority.change(pool, PASSPHRASE, "a new one altogether")

    # The passphrase is checked, and then changed before the decision is made.
    from relay.application import confirmation

    revision = await authority.require(world.database, PASSPHRASE)
    other = threading.Thread(target=lambda: asyncio.run(rotated()))
    other.start()
    other.join()

    async def decided_with_the_old_one() -> None:
        confirmation.by_passphrase(revision)
        await world.close("pause", reason="confirmed by a passphrase that is no more")

    import contextvars

    from relay.domain.story import ActionRefused

    with pytest.raises(ActionRefused, match="passphrase was changed"):
        await asyncio.create_task(decided_with_the_old_one(), context=contextvars.copy_context())
    assert await world.column("SELECT closed_by FROM features") == [None]


async def test_a_story_that_is_not_being_worked_on_is_not_rejected_nor_prepared_to_be(
    desk: Desk,
) -> None:
    """3C-S2-1.6: S2 is planned while S1 is delivered. A rejection of S2 is refused, and
    cannot be prepared to take effect once S2 is released at the same revision."""
    from relay.domain.story import HumanAction
    from tests.integration.fixtures import PRODUCT_OWNER
    from tests.integration.story_world import GREETS

    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    assert await world.story("S2") is StoryState.PLANNED

    prepared = desk.anyone("prepare", "reject", "S2", "--reason", "not wanted", "--why", "early")
    direct = desk.po("reject", "S2", "--reason", "not wanted")

    assert prepared.code == 1 and "not being worked on" in prepared.printed
    assert direct.code == 1 and "not being worked on" in direct.printed
    # And where it is decided: a rejection that names the state it was shown in is stale
    # once the story has left that state, at the same revision and basis.
    as_planned = HumanAction(
        PRODUCT_OWNER, "r-1", "S1", "reject",
        {"reason": "x", "revision": str(await world.revision("S1")),
         "basis": str(await world.basis("S1")), "state": "planned"},
    )  # fmt: skip
    stale = await world.stories.reject(PROJECT, as_planned)
    assert stale.disposition == "stale"
    assert await world.story("S1") is StoryState.RELEASED


async def test_status_and_the_inbox_agree_about_who_can_act(desk: Desk) -> None:
    """3C-S2-1.13: work that asked is waiting only while its question is open, and work
    stopped on a call is waiting for the Product Owner only while they are still asked."""
    from tests.integration.test_inbox import parked_call
    from tests.integration.test_invocations import Calling

    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await parked_call(calling)
        waiting = desk.read("status")
        assert waiting["waiting_for_the_product_owner"] == 1
        assert [each["state"] for each in waiting["owed"] if each["work"] == "build"] == [
            "stopped, waiting for the Product Owner"
        ]
        [item] = desk.read("inbox")

        assert desk.po("set-aside", item["handle"]).code == 0

        after = desk.read("status")
        assert after["waiting_for_the_product_owner"] == 0
        assert [each["state"] for each in after["owed"] if each["work"] == "build"] == [
            "stopped, and set aside by the Product Owner"
        ]
        assert "waiting for the Product Owner: nothing" in desk.anyone("status").out
    finally:
        await calling.close()


async def test_the_ledger_in_text_says_what_each_event_recorded_and_the_roadmap_is_the_one_delivering(
    desk: Desk,
) -> None:
    """3C-S2-1.14: the text ledger has the facts the JSON has. And with one feature being
    delivered and a later one being planned, the roadmap shown by default is the first."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    assert desk.po("request-changes", str(gate), "--reason", "split the story in two").code == 0
    await world.decide(await world.revise(STANDARD_RATE))
    assert desk.po("feature", "start", "a later feature").code == 0

    text = desk.anyone("ledger").out
    by_default = desk.read("roadmap")

    assert "split the story in two" in text  # the reason, as the JSON has it
    assert any(
        "split the story in two" in json.dumps(each["payload"]) for each in desk.read("ledger")
    )
    assert by_default["feature"] == "F1" and by_default["state"] == "delivering"
    assert desk.read("roadmap", "F2")["feature"] == "F2"


async def test_a_prepared_action_means_the_item_it_named_whatever_is_waiting_when_it_is_confirmed(
    desk: Desk,
) -> None:
    """3C-S2-1.10: prepared by the start of an identity while that start was one item's
    alone. More items arrive that begin the same way. The code still means its item."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    first = await world.ask()
    [item] = desk.read("inbox")
    code = code_of(
        desk.anyone("prepare", "answer", item["handle"], "the standard rate", "--why", "as briefed")
    )
    for _ in range(12):
        await world.ask()

    confirmed = desk.po("confirm", code)

    assert confirmed.code == 0, confirmed.printed
    async with world.database.connection() as connection:
        answered = await (
            await connection.execute("SELECT question_id FROM questions WHERE state = 'answered'")
        ).fetchall()
    assert [each["question_id"] for each in answered] == [first]


async def test_a_retry_of_a_parked_call_is_held_to_what_the_person_was_shown_of_it(
    desk: Desk, namespace: str
) -> None:
    """3C-S2-1.8, through the command: the person is shown what the work has cost and
    takes their time. A bill for an earlier call arrives meanwhile. They decided on other
    evidence than there now is: nothing is authorised, and they are shown it again."""
    import asyncio
    import threading

    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL
    from tests.integration.test_inbox import parked_call
    from tests.integration.test_invocations import Calling

    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await parked_call(calling)
        [item] = desk.read("inbox")

        async def a_bill_arrives() -> None:
            async with open_pool(DATABASE_URL, schema=namespace) as pool, pool.connection() as c:
                await c.execute("UPDATE invocations SET provider_cost_usd = 0.40 WHERE attempt = 1")

        def while_the_person_looks() -> None:
            other = threading.Thread(target=lambda: asyncio.run(a_bill_arrives()))
            other.start()
            other.join()

        late = at_a_terminal(
            ["retry", item["handle"]],
            [PASSPHRASE],
            desk.shell,
            desk.here,
            before_typing=while_the_person_looks,
        )

        assert late.code == 1 and "stale" in late.printed, late.printed
        assert await world.column(
            "SELECT count(*) FROM invocations WHERE retry_authorised_by IS NOT NULL"
        ) == [0]
        again = desk.po("retry", item["handle"])
        assert again.code == 0 and '"charged_usd": "0.4' in again.shown
    finally:
        await calling.close()


async def test_a_code_taken_in_time_is_settled_by_its_confirmation_and_by_nothing_else(
    desk: Desk,
) -> None:
    """3C-S2-2.2: one durable answer. A confirmation took the code before its hour and
    has not yet decided: it is waiting for a lock, or its process died. The hour passes.
    Nobody is told the code expired; a second terminal, or the same one again, carries
    the same confirmation on and is given the same receipt."""
    from relay.application import prepared

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))
    request = f"confirm:{code}"
    assert await prepared.consume(world.database, code, request)  # taken, and then: nothing
    # In its hour still, and another request cannot take it: it is the first one's.
    assert not await prepared.consume(world.database, code, "confirm:another")
    assert await prepared.consume(world.database, code, request)  # and the first may again
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE prepared_actions SET expires_at = now() - interval '1 minute'"
        )

    made = await prepared.find(world.database, code)
    await prepared.end(world.database, code, "this expired unused")
    assert made is not None and not made.expired
    assert (await prepared.find(world.database, code)).ended is None  # type: ignore[union-attr]
    assert not await prepared.consume(world.database, code, "confirm:another")
    carried_on = desk.po("confirm", code)

    assert carried_on.code == 0 and "effective" in carried_on.printed, carried_on.printed
    assert await world.column("SELECT state FROM gates") == ["decided"]
    again = desk.anyone("confirm", code)
    assert again.code == 0 and "as on record" in again.printed


async def test_a_confirmation_is_answered_only_by_the_receipt_of_what_was_prepared(
    desk: Desk,
) -> None:
    """3C-S2-2.3: `confirm:<code>` is the name of a code's own request. No command may
    choose it, and a receipt already on record under it for something else is not this
    code's: the prepared gate stays open and nothing reports success."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))

    chosen = desk.po("pause", "F1", "--reason", "mine", "--request-id", f"confirm:{code}")

    assert chosen.code == 1 and "does not begin with `confirm:`" in chosen.printed
    async with world.database.connection() as connection:
        # As a record left by an earlier build, or by a caller of the service, would be.
        await connection.execute(
            "INSERT INTO human_actions (principal, request_id, target, action, payload, digest, "
            "disposition, outcome) VALUES ('product-owner', %s, %s, 'pause', %s::jsonb, 'd', "
            "'effective', 'paused')",
            (f"confirm:{code}", f"{PROJECT}/F1", json.dumps({"reason": "mine"})),
        )
    claimed = desk.po("confirm", code)

    assert claimed.code == 1 and "a request for something else" in claimed.printed
    assert "effective" not in claimed.printed and "passphrase" not in claimed.shown
    assert await world.column("SELECT state FROM gates") == ["open"]


async def test_a_request_made_by_an_earlier_build_is_still_answered_by_its_receipt(
    desk: Desk,
) -> None:
    """3C-S2-2.4: the handle was bound, then, to a digest worked out another way. The
    receipt says what the request was for, and the same words are answered by it."""
    import hashlib

    world = desk.world
    started = desk.po("feature", "start", "prices need VAT")
    handle = started.printed.split()[1]
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE request_handles SET asked = %s WHERE request_id = %s",
            (hashlib.sha256(f"{PROJECT}:start:prices need VAT".encode()).hexdigest(), handle),
        )

    again = desk.anyone("feature", "start", "prices need VAT", "--request-id", handle)
    other = desk.anyone("feature", "start", "something else", "--request-id", handle)

    assert again.code == 0 and "started F1 (as on record)" in again.printed
    assert other.code == 1 and "a request for something else" in other.printed
    assert await world.column("SELECT count(*) FROM features") == [1]


async def test_a_request_of_another_installation_is_not_answered_from_this_one(
    desk: Desk,
) -> None:
    """3C-S2-2.5: two installations may each have a project and a request of the same
    names. The journal says which installation a request was made of."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    approved = desk.po("approve", str(gate))
    handle = approved.printed.split()[1]
    file = Path(desk.shell["RELAY_STATE_DIR"]) / "requests.jsonl"
    elsewhere = "01a20000-0000-7000-8000-00000000000b"
    file.write_text(
        "\n".join(
            line.replace(json.loads(line)["installation"], elsewhere)
            for line in file.read_text().splitlines()
        )
        + "\n"
    )

    replayed = desk.anyone("approve", str(gate), "--request-id", handle)

    assert replayed.code == 1 and f"made of installation {elsewhere}" in replayed.printed
    assert "effective" not in replayed.printed


async def test_no_word_is_taken_that_the_decision_has_no_place_for(desk: Desk) -> None:
    """3C-S2-2.7: refused before a code is given or a passphrase asked, not after."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)

    texted = desk.anyone("prepare", "approve", str(gate), "some text", "--why", "because")
    reasoned = desk.po("approve", str(gate))  # the plain form is taken
    assert texted.code == 1 and "carries no text" in texted.printed
    assert reasoned.code == 0

    from tests.integration.test_inbox import parked_call
    from tests.integration.test_invocations import Calling

    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await parked_call(calling)
        [item] = desk.read("inbox")
        with_a_reason = desk.po("retry", item["handle"], "--reason", "worth another")
        assert with_a_reason.code == 1 and "carries no reason" in with_a_reason.printed
        assert "passphrase" not in with_a_reason.shown
        assert desk.po("retry", item["handle"]).code == 0
    finally:
        await calling.close()


async def test_what_a_model_wrote_is_shown_safely_by_the_reading_commands_and_whole_in_json(
    desk: Desk,
) -> None:
    """3C-S2-2.11: a worker-authored roadmap cannot alter the terminal of the person
    inspecting it. The values themselves are whole in `--json`. And a proposal is named
    by its whole identity (3C-S2-2.10)."""
    from relay.application.stories import StoryDraft

    world = desk.world
    hostile = StoryDraft(
        "S1", "VAT\x1b[2J\x1b[Hat standard rate", "the shopper\rsees gross", {"AC1": "120\x07"}
    )
    proposal = await world.roadmap(hostile)
    assert f"relay show {proposal}`" in desk.anyone("roadmap").out
    await world.decide(proposal)

    status, roadmap = desk.anyone("status").out, desk.anyone("roadmap").out

    for text in (status, roadmap):
        assert "\x1b" not in text and "\r" not in text and "\x07" not in text
    assert "VAT\\x1b[2J\\x1b[Hat standard rate" in status
    assert "the shopper\\x0dsees gross" in roadmap and "120\\x07" in roadmap
    assert desk.read("roadmap")["stories"][0]["title"] == hostile.title  # whole, in JSON


async def test_the_roadmap_shown_by_default_is_of_the_feature_that_holds_the_place_in_delivery(
    desk: Desk,
) -> None:
    """3C-S2-2.10: two features both read `delivering`, and only one of them has a story
    being worked on."""
    from tests.integration.test_completion_histories import two_features_at_work

    world = desk.world
    assert isinstance(world, CompletionWorld)
    desk.shell["RELAY_PROJECT"] = PROJECT  # the Builder's work leaves the checkout on no branch
    await two_features_at_work(world)

    assert desk.read("roadmap")["feature"] == "F2"
    assert desk.read("roadmap", "F1")["feature"] == "F1"


async def test_two_terminals_confirming_one_code_are_given_one_answer(
    desk: Desk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """3C-S2-2.2, the live overlap: the second terminal looked for a receipt and found
    none, and by the time it looks at the record the first has confirmed. What it finds
    has moved because this very code was carried out. It is given that receipt, and the
    code is not marked stale."""
    import argparse

    from relay.application import prepared
    from relay.cli import deciding, main

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))
    assert desk.po("confirm", code).code == 0  # the first terminal
    for name, value in desk.shell.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(desk.here)
    looked, real = [0], deciding.receipt_on_record

    async def not_there_the_first_time(*arguments: Any) -> Any:
        # As the second terminal saw it: nothing on record when it first looked.
        looked[0] += 1
        return None if looked[0] == 1 else await real(*arguments)

    monkeypatch.setattr(deciding, "receipt_on_record", not_there_the_first_time)
    capsys.readouterr()

    code_exit = await main._confirm(argparse.Namespace(code=code, project=None), ["confirm", code])

    said = capsys.readouterr().out
    assert code_exit == 0 and "effective" in said and "as on record" in said, said
    assert "stale" not in said
    kept = await prepared.find(world.database, code)
    assert kept is not None and kept.ended is None


async def test_a_receipt_is_its_projects_and_a_feature_is_named_in_full(desk: Desk) -> None:
    """3C-S2-3.2: from another state directory, someone who means project B repeats the
    handle, the verb, the feature's name and the reason of a cancellation in A. It is A's
    receipt, and is not given to them. A feature's name is not abbreviated."""
    from relay.cli import deciding

    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.behaviours.open_project("another", str(world.remote), "next")
    cancelled = desk.po("cancel", "F1", "--reason", "no longer wanted")
    handle = cancelled.printed.split()[1]
    pool = world.database

    mine = await deciding.receipt_on_record(
        pool, handle, PROJECT, "cancel", "F1", None, "no longer wanted"
    )
    assert mine is not None and mine.outcome == "cancelled"
    for project, name in (("another", "F1"), (PROJECT, "F"), (PROJECT, "F1x")):
        with pytest.raises(deciding.CannotDecide, match="a request for something else"):
            await deciding.receipt_on_record(
                pool, handle, project, "cancel", name, None, "no longer wanted"
            )
    elsewhere = {
        **desk.shell,
        "RELAY_PROJECT": "another",
        "RELAY_STATE_DIR": desk.shell["RELAY_STATE_DIR"] + "-2",
    }
    replayed = with_no_terminal(
        ["cancel", "F1", "--reason", "no longer wanted", "--request-id", handle],
        elsewhere,
        desk.here,
    )
    assert replayed.code == 1 and "effective" not in replayed.printed


async def test_a_request_sent_again_exactly_as_it_was_typed_is_answered_whatever_it_named(
    desk: Desk,
) -> None:
    """3C-S2-3.3: a decision named as FEATURE/Dnumber, and an answer typed with space
    around it, are each answered by their receipt once what they were about has closed."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    question = await world.ask()

    answered = desk.po("answer", str(question), "  the standard rate, always  ")
    replaced = desk.po("replace-decision", "F1/D1", "the reduced rate for books")

    assert answered.code == 0 and replaced.code == 0, answered.printed + replaced.printed
    assert await world.column("SELECT text FROM decisions ORDER BY number") == [
        "the standard rate, always",  # trimmed once: shown, decided on and recorded alike
        "the reduced rate for books",
    ]
    again = desk.anyone(
        "answer", str(question), "  the standard rate, always  ",
        "--request-id", answered.printed.split()[1],
    )  # fmt: skip
    renamed = desk.anyone(
        "replace-decision", "F1/D1", "the reduced rate for books",
        "--request-id", replaced.printed.split()[1],
    )  # fmt: skip
    assert again.code == 0 and "as on record" in again.printed, again.printed
    assert renamed.code == 0 and "as on record" in renamed.printed, renamed.printed
    # And a confirmed code whose text was typed with space around it.
    second = await world.ask()
    code = code_of(desk.anyone("prepare", "answer", str(second), "  zero  ", "--why", "as briefed"))
    assert desk.po("confirm", code).code == 0
    assert "as on record" in desk.anyone("confirm", code).printed


async def test_a_terminal_that_did_not_settle_a_code_does_not_say_how_it_was_settled(
    desk: Desk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """3C-S2-3.4: this terminal read the code as unused and past its hour. Another had
    taken it in time, between that reading and now. Its attempt to end the code changes
    nothing, and it does not report an expiry that did not happen: it carries on as far
    as the person, as the other confirmation would."""
    import argparse
    import dataclasses

    from relay.application import prepared
    from relay.cli import main

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))
    assert await prepared.consume(world.database, code, f"confirm:{code}")  # taken in time
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE prepared_actions SET expires_at = now() - interval '1 minute'"
        )
    for name, value in desk.shell.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(desk.here)
    read, looked = prepared.find, [0]

    async def as_it_was_before_the_other_took_it(pool: Any, of: str) -> Any:
        looked[0] += 1
        now = await read(pool, of)
        assert now is not None
        return dataclasses.replace(now, expired=True, consumed_by=None) if looked[0] == 1 else now

    monkeypatch.setattr(prepared, "find", as_it_was_before_the_other_took_it)
    capsys.readouterr()

    code_exit = await main._confirm(argparse.Namespace(code=code, project=None), ["confirm", code])

    said = capsys.readouterr()
    assert "expired" not in said.out + said.err
    assert code_exit == 1 and "needs a person at a terminal" in said.err  # as far as the person
    kept = await read(world.database, code)
    assert kept is not None and kept.ended is None


async def test_two_identical_decisions_sent_at_once_are_both_given_the_one_receipt(
    desk: Desk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """3C-S2-3.5: the second looked for a receipt while the first was still deciding. By
    the time it looks at the gate, the first has approved and the gate is closed. It is
    given the approval's receipt, and one decision has taken effect."""
    import argparse

    from relay.cli import deciding, main

    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    approved = desk.po("approve", str(gate))
    handle = approved.printed.split()[1]
    for name, value in desk.shell.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(desk.here)
    looked, real = [0], deciding.receipt_on_record

    async def not_there_the_first_time(*arguments: Any) -> Any:
        looked[0] += 1
        return None if looked[0] == 1 else await real(*arguments)

    monkeypatch.setattr(deciding, "receipt_on_record", not_there_the_first_time)
    capsys.readouterr()
    asked = argparse.Namespace(
        verb="approve", target=str(gate), project=None, request_id=handle, command="approve"
    )

    code_exit = await main._decide(asked, ["approve", str(gate), "--request-id", handle])

    said = capsys.readouterr().out
    assert code_exit == 0 and "effective" in said and "as on record" in said, said
    assert await world.column("SELECT count(*) FROM human_actions WHERE action = 'approve'") == [1]


async def test_no_verb_takes_a_word_it_has_no_place_for(desk: Desk) -> None:
    """3C-S2-3.7: asked before the verb is dispatched, so a feature's cancellation and a
    decision's replacement are held to it as an item's decision is."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    question = await world.ask()
    await world.answer(question)

    extra_text = desk.anyone(
        "prepare", "cancel", "F1", "some text", "--reason", "dropped", "--why", "asked"
    )
    extra_reason = desk.anyone(
        "prepare", "replace-decision", "F1/D1", "the reduced rate", "--reason", "why not",
        "--why", "asked",
    )  # fmt: skip
    resumed_with_a_reason = desk.anyone("prepare", "resume", "F1", "--reason", "x", "--why", "y")

    assert extra_text.code == 1 and "carries no text" in extra_text.printed
    assert extra_reason.code == 1 and "carries no reason" in extra_reason.printed
    assert resumed_with_a_reason.code == 1 and "carries no reason" in resumed_with_a_reason.printed
    assert await world.column("SELECT count(*) FROM prepared_actions") == [0]


async def test_no_command_may_take_the_name_a_confirmation_gives_its_request(desk: Desk) -> None:
    """3C-S2-3.8: asked where every caller-chosen handle is admitted. A pending code's
    confirmation is not shut out by an installation command that took its name."""
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    code = code_of(desk.anyone("prepare", "approve", str(gate), "--why", "it looks right"))

    stated = desk.po("policy", "state", "none applies", "--request-id", f"confirm:{code}")
    rotated = desk.anyone("passphrase", "--request-id", f"confirm:{code}")
    probed = desk.anyone("doctor", "--boundary", "--request-id", f"confirm:{code}")

    for ran in (stated, rotated):
        assert ran.code == 1 and "does not begin with `confirm:`" in ran.printed
    assert probed.code == 1
    assert await world.column("SELECT count(*) FROM request_handles") == [0]
    assert await authority.policy_of(world.database) is None
    assert desk.po("confirm", code).code == 0  # and the code is confirmed as it should be


async def test_an_action_prepared_by_an_earlier_build_is_not_stale_for_how_it_was_described(
    desk: Desk,
) -> None:
    """3C-S2-3.9: prepared by a build that described a feature's standing in fewer words.
    Nothing of the feature has changed. It is confirmed, not marked stale."""
    from relay.application import prepared

    world = desk.world
    await world.stories.open_feature(
        (await _po()), PROJECT, "F1", "a newly opened feature with no gate and no pause"
    )
    made = await prepared.prepare(
        world.database, PROJECT, "pause", "F1", None, "waiting on legal", "asked to",
        "as that build showed it", "planning/open/-",
    )  # fmt: skip

    confirmed = desk.po("confirm", made.code)

    assert confirmed.code == 0 and "effective: paused" in confirmed.printed, confirmed.printed
    # One that has changed since is stale still, in whichever words.
    later = await prepared.prepare(
        world.database, PROJECT, "cancel", "F1", None, "dropped", "asked to",
        "as that build showed it", "planning/open/-",
    )  # fmt: skip
    stale = desk.po("confirm", later.code)
    assert stale.code == 1 and "stale" in stale.printed


async def _po() -> Any:
    from tests.integration.fixtures import PRODUCT_OWNER

    return PRODUCT_OWNER


async def test_status_says_who_takes_up_work_that_is_the_control_planes(desk: Desk) -> None:
    """3C-S2-3.10: a red, an integration and a final check are not a worker's to claim.
    With the control plane stopped, starting another worker moves none of them on."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    async with world.database.connection() as connection:
        kinds = await (
            await connection.execute(
                "SELECT kind FROM assignments WHERE state = 'wanted' AND grant_id IS NULL"
            )
        ).fetchall()

    owed = {each["work"]: each["state"] for each in desk.read("status")["owed"]}

    assert {each["kind"] for each in kinds} & {"red", "integrate", "complete"} or owed
    for work, state in owed.items():
        if work in ("red", "integrate", "complete"):
            assert state == "waiting for the control plane (`relay up`)"
        elif state.startswith("waiting for a"):
            assert state == "waiting for a worker"
