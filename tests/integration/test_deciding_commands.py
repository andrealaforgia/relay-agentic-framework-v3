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
