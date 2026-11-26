"""The steps of a story carried out by workers: what the Verifier specifies and vouches
for, what the Analyst assesses, what each reconfirms after a decision, and the reds the
control plane observes for itself. Each history is driven by workers and a person only."""

from __future__ import annotations

from typing import Any

import pytest

from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused
from tests.integration.fixtures import (
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
)
from tests.integration.test_review_task import NOTHING_FOUND
from tests.integration.test_roadmap_task import ASKING, S1, proposing
from tests.integration.test_workers_deliver import (
    B1,
    DEMONSTRATED,
    STANDARD_RATE_CONTRACT,
    VOUCHED,
    Delivering,
    Role,
    builds,
    delivering,
)
from tests.integration.trunk_world import PROJECT

STANDS = delivering(verdict="stands")
ALREADY_PASSES = {
    **STANDARD_RATE_CONTRACT,
    "expect_stdout": "100\n",
    "baseline_stdout": "120\n",
}


def staffed(**changed: Any) -> dict[str, Role]:
    """Every role, scripted for one story delivered without incident, unless told
    otherwise."""
    script: dict[str, dict[str, list[Any]]] = {
        "analyst": {
            "You_propose_the_roadmap": [proposing(S1)],
            "You_are_asked_to_assess": [VOUCHED],
            "You_are_asked_whether": [STANDS, STANDS, STANDS],
        },
        "verifier": {
            "You_decide_how_a_user_story_is_shown": [B1, DEMONSTRATED],
            "You_are_asked_to_vouch": [VOUCHED],
            "You_are_asked_whether": [STANDS, STANDS],
        },
        "builder": {"You_are_the_Builder": [builds({"vat.py": VAT_APPLIES_STANDARD_RATE})]},
        "qa": {"You_are_reviewing": [NOTHING_FOUND]},
        "sentinel": {"You_are_reviewing": [NOTHING_FOUND]},
    }
    for path, replies in changed.items():
        role, marker = path.split("__")
        script[role][marker] = replies
    return {role: Role(**replies) for role, replies in script.items()}


async def released(at: Delivering, **changed: Any) -> dict[str, Role]:
    """A feature whose one story is released, with nothing yet specified."""
    roles = staffed(**changed)
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    await at.world.decide(await at.world.gate())
    await at.staff(**{role: each for role, each in roles.items() if role != "analyst"})
    return roles


def briefings(role: Role, marker: str) -> list[str]:
    return [each.briefing for each in role.asked if marker in each.briefing]


async def kinds(at: Delivering, state: str = "done") -> list[object]:
    return await at.world.column(
        f"SELECT kind FROM assignments WHERE state = '{state}' ORDER BY created_at"
    )


async def test_a_behaviour_is_specified_whole_and_its_red_is_the_control_planes(
    delivering_world: Delivering,
) -> None:
    """S3-20.7 and S3-20.8: no behaviour without its contract, and one red owed for it,
    which no worker can take."""
    at = delivering_world
    await released(at, builder__You_are_the_Builder=[])
    at.workers.pop("builder")
    seen: list[object] = []
    real = at.world.features.reconcile

    async def looking_first(project_id: str, **named: Any) -> None:
        seen.extend(
            await at.world.column("SELECT state || ':' || (contract IS NOT NULL) FROM behaviours")
        )
        seen.extend(await at.world.column("SELECT state FROM assignments WHERE kind = 'red'"))
        await real(project_id, **named)

    for worker in at.workers.values():
        worker._then = looking_first
    await at.work()

    # As the specification was applied, before any pass: all of it, and the red owed.
    assert seen[:2] == ["validating_red:true", "wanted"]
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING
    assert await kinds(at) == ["plan", "specify", "red"]
    assert await kinds(at, "wanted") == ["build"]
    assert await at.world.column(
        "SELECT (expects->>'list_revision')::integer - (admitted_on->>'list_revision')::integer "
        "FROM invocations WHERE role = 'verifier'"
    ) == [1]


async def test_a_red_is_not_a_workers_to_claim(delivering_world: Delivering) -> None:
    at = delivering_world
    await released(at)
    at.world.features._behaviours = None  # so that the red stays owed
    await at.workers["verifier"].carry_out(
        (await at.world.claims.of(await _the(at, "specify"))) or {}
    )
    red = await _the(at, "red")

    with pytest.raises(ActionRefused, match="the control plane takes up"):
        await at.world.claims.claim(VERIFIER, red)


async def _the(at: Delivering, kind: str) -> Any:
    [found] = await at.world.column(
        f"SELECT assignment_id FROM assignments WHERE kind = '{kind}' "
        "AND state IN ('wanted', 'granted')"
    )
    return found


@pytest.mark.parametrize(
    ("contract", "why"),
    [
        ({**STANDARD_RATE_CONTRACT, "baseline_stdout": "120\n"}, "already satisfies"),
        ({**STANDARD_RATE_CONTRACT, "baseline_stderr": "boom"}, "one of stdout or stderr"),
        ({**STANDARD_RATE_CONTRACT, "observes": " "}, "public behaviour"),
    ],
)
async def test_a_contract_that_is_not_one_specifies_nothing_and_is_asked_for_again(
    delivering_world: Delivering, contract: dict[str, Any], why: str
) -> None:
    at = delivering_world
    bad = delivering(**{**B1["delivered"], "contract": contract})
    roles = await released(
        at, verifier__You_decide_how_a_user_story_is_shown=[bad, B1, DEMONSTRATED]
    )

    await at.work()

    assert why in briefings(roles["verifier"], "You decide how")[1]
    assert await at.world.column("SELECT behaviour_id FROM behaviours") == ["B1"]
    [story] = await at.world.stories.inbox(PROJECT)
    assert story.kind == "story"


async def test_a_contract_whose_red_shows_it_cannot_serve_is_the_verifiers_to_write_again(
    delivering_world: Delivering,
) -> None:
    """S3-20.8: an invalid red is observed once, and the Verifier is told why."""
    at = delivering_world
    passes_already = delivering(**{**B1["delivered"], "contract": ALREADY_PASSES})
    rewritten = delivering(demonstrated=False, contract=STANDARD_RATE_CONTRACT)
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[passes_already, rewritten, DEMONSTRATED],
    )

    await at.work()

    second = briefings(roles["verifier"], "You decide how")[1]
    assert "was run on the trunk and cannot serve" in second and '"100\\n"' in second
    assert await at.world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [
        2
    ]  # one of each contract, and none of either twice
    assert await at.world.column("SELECT red_cycle FROM behaviours") == [1]
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"
    await at.world.features.reconcile(PROJECT)
    assert await at.world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [2]


async def test_a_verifier_that_asks_is_answered_and_goes_on_from_the_answer(
    delivering_world: Delivering,
) -> None:
    """S3-18.8 and S3-19.1: one further call for the work that asked, made once what
    the answer put in question has been reconfirmed."""
    at = delivering_world
    roles = await released(
        at, verifier__You_decide_how_a_user_story_is_shown=[ASKING, B1, DEMONSTRATED]
    )
    await at.work()
    [question] = await at.world.decisions.open_questions(PROJECT)
    assert await kinds(at, "asked") == ["specify"]
    assert await kinds(at, "wanted") == []

    await at.world.answer(question.question_id, "zero")
    await at.work()

    asked = briefings(roles["verifier"], "You decide how")
    assert len(asked) == 3
    assert "The Product Owner answered: zero" in asked[1]
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"
    assert await at.world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'specify' AND succeeds IS NOT NULL"
    ) == [1]


async def test_a_story_sent_back_at_its_gate_is_taken_up_again_by_the_verifier(
    delivering_world: Delivering,
) -> None:
    """S3-18.8: with the person's reason, and through to the gate again."""
    at = delivering_world
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        analyst__You_are_asked_to_assess=[VOUCHED, VOUCHED],
    )
    await at.work()

    await at.world.decide(await at.world.gate(), "request_changes", reason="show the total")
    await at.work()

    assert (
        "The Product Owner sent the story back: show the total"
        in (briefings(roles["verifier"], "You decide how")[2])
    )
    [story] = await at.world.stories.inbox(PROJECT)
    assert story.kind == "story"


async def test_a_decision_that_clears_a_seal_is_reconfirmed_and_the_story_reaches_its_gate_again(
    delivering_world: Delivering,
) -> None:
    """S3-19.9: every owner reconfirms, then the rule finds a story with everything on
    trunk and nothing said about it as it now stands."""
    at = delivering_world
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        analyst__You_are_asked_to_assess=[VOUCHED, VOUCHED],
    )
    await at.work()
    first = await at.world.gate()

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.work()

    assert await at.world.gate() != first
    assert len(briefings(roles["verifier"], "You are asked whether")) == 1
    assert len(briefings(roles["analyst"], "You are asked whether")) == 1
    assert await kinds(at, "wanted") == []


async def test_a_contract_amended_after_a_decision_has_its_red_observed_and_is_built_again(
    delivering_world: Delivering,
) -> None:
    """S3-19.9: the amendment owes the red, by the behaviour's own identity."""
    at = delivering_world
    amended = delivering(
        verdict="amended",
        contract={**STANDARD_RATE_CONTRACT, "contract_id": "B1-as-decided"},
    )
    await released(
        at,
        verifier__You_are_asked_whether=[amended],
        builder__You_are_the_Builder=[builds({"vat.py": VAT_APPLIES_STANDARD_RATE})],
    )
    at.workers.pop("builder")
    await at.work()
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.work()

    assert await at.world.column("SELECT contract->>'contract_id', red_cycle FROM behaviours") == [
        "B1-as-decided"
    ]
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING
    assert await at.world.column(
        "SELECT state FROM assignments WHERE kind = 'red' ORDER BY created_at"
    ) == ["done", "done"]
    assert await kinds(at, "wanted") == ["build"]


async def test_what_a_story_needs_is_owed_while_its_feature_is_paused_and_claimed_after(
    delivering_world: Delivering,
) -> None:
    """S3-20.6: a pause stops work being claimed, not being owed."""
    at = delivering_world
    await released(at)
    at.workers.pop("verifier")
    await at.world.close("pause", reason="budget review")
    await at.world.column("DELETE FROM assignments WHERE kind = 'specify' RETURNING 1")

    await at.world.features.reconcile(PROJECT)
    await at.world.features.reconcile(PROJECT)

    [specify] = await at.world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'specify'"
    )
    with pytest.raises(ActionRefused, match="paused"):
        await at.world.claims.claim(VERIFIER, specify)  # type: ignore[arg-type]
    await at.world.close("resume")
    await at.world.claims.claim(VERIFIER, specify)  # type: ignore[arg-type]
