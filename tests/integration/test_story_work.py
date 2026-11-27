"""The steps of a story carried out by workers: what the Verifier specifies and vouches
for, what the Analyst assesses, what each reconfirms after a decision, and the reds the
control plane observes for itself. Each history is driven by workers and a person only."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relay.application import authoring as authoring_module
from relay.application import behaviours as behaviours_module
from relay.application.authoring import _refusing, contract_from
from relay.application.behaviours import rewrite_contract_in
from relay.application.invocations import Effect
from relay.application.stories import owe_a_revision
from relay.application.supersessions import Supersessions
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.domain.story import ActionRefused, HumanAction, SealRefused
from relay.git.checkout import CheckoutFailed
from tests.integration.fixtures import (
    BUILDER,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
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
        ({**STANDARD_RATE_CONTRACT, "baseline_stderr": "boom"}, "exactly one of stdout or stderr"),
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
    # Until its owners have reconfirmed it, the story's next step is not known: the
    # pass that follows the answer owes the reconfirmations and nothing else.
    assert set(await kinds(at, "wanted")) == {"reconfirm"}
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


async def snapshots(at: Delivering) -> list[list[object]]:
    """What is wanted each time something has been applied, before the pass that
    follows it: what the apply's own transaction owed, and not the pass after it."""
    seen: list[list[object]] = []
    real = at.world.features.reconcile

    async def looking_first(project_id: str, **named: Any) -> None:
        seen.append(
            await at.world.column(
                "SELECT kind FROM assignments WHERE state = 'wanted' ORDER BY created_at"
            )
        )
        await real(project_id, **named)

    for worker in at.workers.values():
        worker._then = looking_first
    return seen


async def test_each_step_owes_the_next_in_its_own_transaction_and_not_only_by_the_pass(
    delivering_world: Delivering,
) -> None:
    at = delivering_world
    await released(at)
    seen = await snapshots(at)

    await at.work()

    assert ["red"] in seen  # the behaviour specified
    assert ["seal"] in seen  # the story said to be demonstrated
    assert ["assess"] in seen  # the list sealed


async def test_a_contract_written_again_owes_its_red_and_one_that_fails_again_is_written_again(
    delivering_world: Delivering,
) -> None:
    """Each writing is an occasion of its own: the second invalid red finds nothing
    still owed from the first, and owes the contract again."""
    at = delivering_world
    passes_already = delivering(**{**B1["delivered"], "contract": ALREADY_PASSES})
    still_wrong = delivering(
        demonstrated=False, contract={**ALREADY_PASSES, "contract_id": "B1-second"}
    )
    rewritten = delivering(demonstrated=False, contract=STANDARD_RATE_CONTRACT)
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[
            passes_already,
            still_wrong,
            rewritten,
            DEMONSTRATED,
        ],
    )
    seen = await snapshots(at)

    await at.work()

    assert len(briefings(roles["verifier"], "cannot serve")) == 2
    assert seen[:3] == [["red"], ["red"], ["red"]]  # each contract owed its own red
    assert await at.world.column(
        "SELECT state FROM assignments WHERE kind = 'specify' AND behaviour_id = 'B1'"
    ) == ["done", "done"]
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_contract_is_written_again_only_while_its_behaviour_waits_for_a_red(
    delivering_world: Delivering,
) -> None:
    at = delivering_world
    await released(at)
    at.workers.pop("builder")
    await at.work()
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING

    with pytest.raises(ActionRefused, match="not waiting for a red"):
        async with at.world.database.connection() as connection, connection.transaction():
            await rewrite_contract_in(
                connection, VERIFIER, PROJECT, "B1", contract_from(ALREADY_PASSES), basis=0
            )


async def test_a_contract_registered_by_hand_owes_its_red_too(
    delivering_world: Delivering,
) -> None:
    at = delivering_world
    await released(at)
    behaviours = at.world.behaviours

    await behaviours.propose(
        VERIFIER, PROJECT, "S1", "B9", "by hand", ["AC1"], basis=0, story_revision=1
    )
    await behaviours.begin_test_design(VERIFIER, PROJECT, "B9")
    await behaviours.register_acceptance_test(
        VERIFIER, PROJECT, "B9", contract_from(STANDARD_RATE_CONTRACT), basis=0
    )

    assert await at.world.column(
        "SELECT state FROM assignments WHERE kind = 'red' AND behaviour_id = 'B9'"
    ) == ["wanted"]
    # And the specification the story was owed is finished: its behaviour has been said.
    assert await kinds(at, "wanted") == ["red"]


async def test_an_assessment_of_a_seal_that_is_gone_is_not_paid_for(
    delivering_world: Delivering,
) -> None:
    at = delivering_world
    roles = await released(at)

    class WithTheSealGone(Role):
        async def invoke(self, request: Any, **named: Any) -> Any:
            return await Role.invoke(roles["analyst"], request, **named)

    analyst = at.workers.pop("analyst")
    await at.work()
    [assess] = await at.world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'assess' AND state = 'wanted'"
    )
    await at.world.column("UPDATE stories SET seal_id = NULL, sealed_revision = NULL RETURNING 1")
    at.workers["analyst"] = analyst

    left = await analyst.carry_out((await at.world.claims.of(assess)) or {})  # type: ignore[arg-type]

    assert left == "refused: S1 has no seal to assess yet"
    assert await at.world.column(
        "SELECT count(*) FROM invocations v JOIN assignments a USING (assignment_id) "
        "WHERE a.kind = 'assess'"
    ) == [0]
    assert await at.world.column("SELECT count(*) FROM story_assessments") == [0]


async def test_a_reconfirmation_that_asks_is_not_owed_again_by_the_answer(
    delivering_world: Delivering,
) -> None:
    """The answer is a decision, and owes the reconfirmations of its own basis."""
    at = delivering_world
    await released(at, analyst__You_are_asked_whether=[ASKING, STANDS, STANDS])
    at.workers.pop("builder")
    await at.work()

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.work()
    [question] = await at.world.decisions.open_questions(PROJECT)
    await at.world.answer(question.question_id, "zero")
    await at.work()

    assert await at.world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'reconfirm' AND succeeds IS NOT NULL"
    ) == [0]
    assert await at.world.column(
        "SELECT state, count(*) FROM assignments WHERE kind = 'reconfirm' AND role = 'analyst' "
        "GROUP BY state ORDER BY state"
    ) == ["asked", "done"]
    assert await kinds(at, "wanted") == ["build"]


async def test_a_red_found_wanting_is_not_observed_again_while_its_contract_waits_to_be_rewritten(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.2: however many passes run before the Verifier replies."""
    at = delivering_world
    passes_already = delivering(**{**B1["delivered"], "contract": ALREADY_PASSES})
    await released(at, verifier__You_decide_how_a_user_story_is_shown=[passes_already])
    await at.workers["verifier"].carry_out(
        (await at.world.claims.of(await _the(at, "specify"))) or {}
    )
    assert await at.world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [1]

    await at.world.features.reconcile(PROJECT)
    await asyncio.gather(at.world.features.reconcile(PROJECT), at.world.features.reconcile(PROJECT))

    assert await at.world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [1]
    assert await at.world.column(
        "SELECT kind || ':' || state FROM assignments WHERE behaviour_id = 'B1' ORDER BY created_at"
    ) == ["red:done", "specify:wanted"]


async def test_two_passes_at_once_observe_a_red_once(delivering_world: Delivering) -> None:
    """3B-S3-1.2: the red is taken before it is launched."""
    at = delivering_world
    await released(at)
    at.world.features._behaviours = None  # so that the red is still owed
    await at.workers["verifier"].carry_out(
        (await at.world.claims.of(await _the(at, "specify"))) or {}
    )

    await asyncio.gather(
        at.world.behaviours.observe_owed_reds(PROJECT),
        at.world.behaviours.observe_owed_reds(PROJECT),
    )

    assert await at.world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [1]
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING


async def test_a_story_is_not_said_to_be_demonstrated_while_a_criterion_has_nothing_to_show_it(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.5: the rule a seal is held to is held to where the saying is accepted."""
    at = delivering_world
    roles = await released(
        at, verifier__You_decide_how_a_user_story_is_shown=[DEMONSTRATED, B1, DEMONSTRATED]
    )

    await at.work()

    assert (
        "S1 is not demonstrated yet: a story with no behaviours demonstrates nothing"
        in (briefings(roles["verifier"], "You decide how")[1])
    )
    assert await at.world.column("SELECT count(*) FROM assignments WHERE kind = 'seal'") == [1]
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_behaviour_named_as_one_there_already_is_specifies_nothing(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.6: a refusal of the reply, on record, and the next reply is used."""
    at = delivering_world
    roles = await released(
        at, verifier__You_decide_how_a_user_story_is_shown=[B1, B1, DEMONSTRATED]
    )

    await at.work()

    assert (
        "B1 is already a behaviour of this project"
        in (briefings(roles["verifier"], "You decide how")[2])
    )
    assert await at.world.column("SELECT behaviour_id FROM behaviours") == ["B1"]
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_reply_that_asks_nothing_is_no_result_whoever_hands_it_to_the_record(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.6: the schema allows it, and what the schema cannot say is checked where
    a result is recorded, not only by the worker that would have refused it first."""
    at = delivering_world
    asks_nothing = {"outcome": "question", "assumptions": []}
    await released(at, verifier__You_decide_how_a_user_story_is_shown=[asks_nothing, B1])
    verifier = at.workers["verifier"]
    at.workers.pop("builder")  # the story goes no further than its first behaviour

    class TakesAnything:
        """The Verifier's task, as a worker that checked nothing itself would use it."""

        def __init__(self, task: Any) -> None:
            self._task = task

        def __getattr__(self, name: str) -> Any:
            return getattr(self._task, name)

        def problem_with(self, reply: Any) -> None:
            return None

    verifier._tasks = {kind: TakesAnything(task) for kind, task in verifier._tasks.items()}  # type: ignore[assignment]

    await at.work()
    await at.world.features.reconcile(PROJECT)

    assert await at.world.column(
        "SELECT state || ': ' || reason FROM invocations WHERE role = 'verifier' ORDER BY attempt"
    ) == ["rejected: a reply that asks has a question and delivers nothing", None]
    assert await at.world.column(
        "SELECT envelope IS NOT NULL FROM invocations WHERE role = 'verifier' ORDER BY attempt"
    ) == [True, True]
    assert await at.world.column("SELECT behaviour_id FROM behaviours") == ["B1"]


@pytest.mark.parametrize("first", ["analyst", "verifier"])
async def test_nothing_is_asked_about_a_story_until_every_owner_has_reconfirmed_what_is_theirs(
    delivering_world: Delivering, first: str
) -> None:
    """3B-S3-1.7: in whichever order the two owners answer."""
    at = delivering_world
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        analyst__You_are_asked_to_assess=[VOUCHED, VOUCHED],
    )
    await at.work()
    await at.world.answer(await at.world.ask(), "zero-rated")
    second = "verifier" if first == "analyst" else "analyst"
    later = at.workers.pop(second)
    authoring = len(briefings(roles["verifier"], "You decide how"))

    await at.work()
    await at.world.features.reconcile(PROJECT)

    assert await kinds(at, "wanted") == ["reconfirm"]
    assert len(briefings(roles["verifier"], "You decide how")) == authoring
    at.workers[second] = later
    await at.work()
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_waiting_proposal_is_reconfirmed_on_what_it_proposes_new_stories_and_all(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.8: a story a proposal adds is no story yet, and is what is vouched for."""
    at = delivering_world
    roles = staffed()
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    first = await at.world.gate()

    await at.world.answer(await at.world.ask(story=None), "prices are in euros")
    await at.work()

    [asked] = briefings(roles["analyst"], "You are asked whether")
    assert "S1 [new] Standard rate: a gross price is shown" in asked
    assert "AC1: 100 net is shown as 120" in asked
    assert "prices are in euros" in asked
    assert await at.world.gate() != first  # the same proposal, put to the person again


async def test_a_contract_on_trunk_that_no_longer_stands_is_replaced_by_workers_and_a_person(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.9: the Verifier declares it, specifies the behaviour that replaces it, the
    Product Owner approves the replacement, and the candidate that contradicts the old
    contract reaches trunk."""
    at = delivering_world
    quarter = {
        **STANDARD_RATE_CONTRACT,
        "contract_id": "B2-quarter",
        "expect_stdout": "125\n",
        "baseline_stdout": "120\n",
    }
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="the rate is a quarter",
        demonstrates=["AC1"],
        contract=quarter,
        replaces="B1",
        replaces_because="the Product Owner decided the rate is 25%",
    )
    applies_a_quarter = "import sys\nprint(int(sys.argv[1]) * 125 // 100)\n"
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, b2, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        verifier__You_are_asked_whether=[delivering(verdict="to_be_replaced")],
        analyst__You_are_asked_to_assess=[VOUCHED, VOUCHED],
        builder__You_are_the_Builder=[
            builds({"vat.py": VAT_APPLIES_STANDARD_RATE}),
            builds({"vat.py": applies_a_quarter}),
        ],
        qa__You_are_reviewing=[NOTHING_FOUND, NOTHING_FOUND],
        sentinel__You_are_reviewing=[NOTHING_FOUND, NOTHING_FOUND],
    )
    await at.work()

    await at.world.answer(await at.world.ask(), "the rate is 25%")
    await at.work()

    assert await at.world.column("SELECT to_be_replaced FROM behaviours ORDER BY position") == [
        True,
        False,
    ]
    [supersession] = await at.world.stories.inbox(PROJECT)
    assert supersession.kind == "supersession"
    assert supersession.manifest["reason"] == "the Product Owner decided the rate is 25%"
    assert "B1 (declared to be replaced)" in briefings(roles["verifier"], "You decide how")[2]
    assert await at.world.state("B2") is BehaviourState.VALIDATING_RED  # nothing built yet

    assert (await at.world.decide(supersession.gate_id)).disposition == "effective"
    assert await at.world.column("SELECT state FROM supersessions") == ["standing"]
    await at.work()

    on_trunk = at.world.git("show", f"{at.world.trunk()}:vat.py", at=at.world.remote)
    assert on_trunk == applies_a_quarter.strip()
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_waiting_proposal_its_author_says_no_longer_stands_is_withdrawn_and_owed_again(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.4: and the work owed to replace it can be claimed."""
    at = delivering_world
    roles = staffed(
        analyst__You_propose_the_roadmap=[proposing(S1), proposing(S1)],
        analyst__You_are_asked_whether=[
            delivering(verdict="to_be_replaced", because="prices are not in pounds")
        ],
    )
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()

    await at.world.answer(await at.world.ask(story=None), "prices are in euros")
    await at.work()

    assert await at.world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "sent_back",
        "proposed",
    ]
    again = briefings(roles["analyst"], "You propose the roadmap")[1]
    assert "the proposal has to be replaced: prices are not in pounds" in again
    assert await kinds(at, "wanted") == []


async def test_a_red_that_could_not_be_observed_is_owed_still_and_observed_by_a_later_pass(
    delivering_world: Delivering, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken before it is launched, and given back when no verdict settled it."""
    at = delivering_world
    await released(at)
    at.workers.pop("builder")

    async def does_not_answer(*_: Any, **__: Any) -> str:
        raise CheckoutFailed("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(behaviours_module, "trunk_head", does_not_answer)
        await at.work()
        assert await at.world.column("SELECT state FROM assignments WHERE kind = 'red'") == [
            "wanted"
        ]
        assert await at.world.state("B1") is BehaviourState.VALIDATING_RED

    await at.world.features.reconcile(PROJECT)

    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING


async def test_what_a_service_raises_to_refuse_a_reply_is_a_refusal_of_the_reply() -> None:
    """3B-S3-1.5: on record with the call, not an error that leaves a paid reply unread."""

    async def nothing(run: Any) -> None:
        return None

    async def refuses(connection: Any, run: Any) -> None:
        raise SealRefused("no behaviour demonstrates ['AC2']")

    async def raises_in_perform(run: Any) -> None:
        raise TransitionRefused("seal", BehaviourState.PROPOSED, (BehaviourState.REVIEW,))

    with pytest.raises(ActionRefused, match="no behaviour demonstrates"):
        settle = _refusing(Effect("seal", nothing, refuses)).settle
        assert settle is not None
        await settle(None, None)  # type: ignore[arg-type]
    with pytest.raises(ActionRefused):
        await _refusing(Effect("seal", raises_in_perform)).perform(None)  # type: ignore[arg-type]


async def test_a_storys_step_already_owed_is_not_claimed_while_a_contract_waits_to_be_reconfirmed(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.7: work owed before the decision, and claimed after the story's own
    reconfirmation, is refused at the claim."""
    at = delivering_world
    await released(
        at, verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, DEMONSTRATED]
    )
    await at.work()
    await at.world.column(
        "UPDATE stories SET seal_id = NULL, sealed_revision = NULL, state = 'released' RETURNING 1"
    )
    await at.world.column("UPDATE gates SET state = 'superseded' RETURNING 1")
    await at.world.features.reconcile(PROJECT)
    seal = await _the(at, "seal")
    at.workers.pop("verifier")

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.work()  # the Analyst reconfirms the story; the contract still waits

    assert seal == await _the(at, "seal")
    with pytest.raises(ActionRefused, match="B1 has yet to be reconfirmed by its owner"):
        await at.world.claims.claim(VERIFIER, seal)


async def test_a_proposal_that_cannot_stand_is_withdrawn_when_its_author_says_it_stands(
    delivering_world: Delivering,
) -> None:
    """3B-S3-1.4: the service says it has to be replaced, and it is not left waiting."""
    at, world = delivering_world, delivering_world.world
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
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
    kept = {"change": "kept", "story_id": "S1"}
    corrected = {**S1, "story_id": "S3", "corrects": "S1"}
    reworded = {**S1, "story_id": "S2", "change": "reworded", "title": "A warmer greeting"}
    roles = staffed(
        analyst__You_propose_the_roadmap=[
            proposing(kept, reworded, corrected),
            proposing(kept, {"change": "kept", "story_id": "S2"}, corrected),
        ],
        analyst__You_are_asked_whether=[STANDS, STANDS, STANDS, STANDS],
    )
    await at.staff(analyst=roles["analyst"])
    await at.work()
    assert await world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "approved",
        "proposed",
    ]

    await world.answer(await world.ask(story=None), "prices are in euros")
    await at.work()

    assert await world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "approved",
        "sent_back",
        "proposed",
    ]
    assert "the proposal has to be replaced" in briefings(roles["analyst"], "You propose")[1]


async def test_a_seal_the_domain_refuses_is_a_reply_that_gave_nothing_and_not_an_error(
    delivering_world: Delivering, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S3-1.5, through a task's own effects: the reply is disposed of, on record."""
    at = delivering_world
    await released(at, verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED, VOUCHED])

    async def refuses(*_: Any, **__: Any) -> None:
        raise SealRefused("no behaviour demonstrates ['AC1']")

    monkeypatch.setattr(authoring_module, "seal_in", refuses)

    await at.work()

    assert set(
        await at.world.column(
            "SELECT v.state || ': ' || v.reason FROM invocations v JOIN assignments a "
            "USING (assignment_id) WHERE a.kind = 'seal'"
        )
    ) == {"rejected: no behaviour demonstrates ['AC1']"}


async def test_a_withdrawal_written_before_a_later_decision_withdraws_nothing_of_what_it_owes(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.1: the reply is of one grant and one basis, and both have gone."""
    at = delivering_world
    roles = staffed(
        analyst__You_are_asked_whether=[
            delivering(verdict="to_be_replaced", because="prices are not in pounds")
        ]
    )
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    await at.world.answer(await at.world.ask(story=None), "prices are in euros")
    analyst = roles["analyst"]
    real = analyst.invoke

    async def while_the_person_decides_again(request: Any, **named: Any) -> Any:
        analyst.invoke = real  # type: ignore[method-assign]
        await at.world.answer(await at.world.ask(story=None), "and rounded to the cent")
        return await real(request, **named)

    analyst.invoke = while_the_person_decides_again  # type: ignore[method-assign]
    stale = await _the(at, "reconfirm")

    left = await at.workers["analyst"].carry_out((await at.world.claims.of(stale)) or {})

    assert left == "overtaken"
    assert await at.world.column("SELECT state FROM roadmaps") == ["proposed"]
    newer = await _the(at, "reconfirm")
    assert newer != stale
    assert await at.world.column(
        f"SELECT (issued_on->>'basis')::integer FROM assignments WHERE assignment_id = '{newer}'"
    ) == [2]


async def test_an_ordinary_withdrawal_is_an_applied_reply(delivering_world: Delivering) -> None:
    """3B-S3-2.1: done with the mark, so the call that did it is not called overtaken."""
    at = delivering_world
    roles = staffed(
        analyst__You_propose_the_roadmap=[proposing(S1), proposing(S1)],
        analyst__You_are_asked_whether=[delivering(verdict="to_be_replaced", because="pounds")],
    )
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    await at.world.answer(await at.world.ask(story=None), "prices are in euros")

    await at.work()

    assert await at.world.column(
        "SELECT v.state FROM invocations v JOIN assignments a USING (assignment_id) "
        "WHERE a.kind = 'reconfirm'"
    ) == ["applied"]
    assert await at.world.column("SELECT state FROM assignments WHERE kind = 'reconfirm'") == [
        "done"
    ]


async def test_a_roadmap_in_force_that_no_longer_stands_is_revised_by_the_analyst(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.6: from a feature-wide answer, through a worker's revision, to its
    approval: nobody has to propose on the Analyst's behalf."""
    at, world = delivering_world, delivering_world.world
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    reworded = {
        "change": "reworded",
        "story_id": "S2",
        "title": "Greeting in French",
        "outcome": "the shopper is greeted in French",
        "criteria": [{"id": "AC1", "text": "bonjour is printed"}],
    }
    roles = staffed(
        analyst__You_propose_the_roadmap=[
            proposing({"change": "kept", "story_id": "S1"}, reworded)
        ],
        analyst__You_are_asked_whether=[
            delivering(verdict="to_be_replaced", because="the greeting is to be in French"),
            STANDS,
            STANDS,
        ],
    )
    await at.staff(analyst=roles["analyst"])

    await world.answer(await world.ask(story=None), "the shop is French")
    await at.work()

    [proposal] = await world.stories.inbox(PROJECT)
    assert proposal.kind == "roadmap"
    [asked] = briefings(roles["analyst"], "You propose the roadmap")
    assert "the roadmap in force no longer stands" in asked
    assert "the greeting is to be in French" in asked
    # 3B-S3-3.9: each briefing says what is so. No final check failed here.
    assert "final check" not in asked
    assert (
        "you will be asked for a revision of it"
        in (briefings(roles["analyst"], "You are asked whether")[0])
    )
    await world.decide(proposal.gate_id)
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == [
        "Greeting in French"
    ]
    assert await world.column("SELECT state FROM assignments WHERE kind = 'revise'") == ["done"]


async def test_an_assessment_that_asked_is_not_asked_for_again_until_the_story_is_sealed_again(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.4: the answer undoes the seal the assessment was of."""
    at = delivering_world
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        analyst__You_are_asked_to_assess=[ASKING, VOUCHED],
    )
    await at.work()
    [question] = await at.world.decisions.open_questions(PROJECT)
    verifier = at.workers.pop("verifier")

    await at.world.answer(question.question_id, "zero")
    await at.work()  # the Analyst first, with everything of its own reconfirmed

    assert len(briefings(roles["analyst"], "You are asked to assess")) == 1
    assert await at.world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'assess' AND succeeds IS NOT NULL"
    ) == [0]
    at.workers["verifier"] = verifier
    await at.work()
    assert len(briefings(roles["analyst"], "You are asked to assess")) == 2
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_work_found_done_when_its_command_comes_again_still_wakes_the_control_plane(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.10: a worker cut short after its result was applied never ran the pass."""
    at = delivering_world
    await released(at)
    specify = (await at.world.claims.of(await _the(at, "specify"))) or {}
    verifier = at.workers["verifier"]
    verifier._then = None  # applied, and stopped before the pass that follows
    assert await verifier.carry_out(specify) == "applied"
    assert await at.world.state("B1") is BehaviourState.VALIDATING_RED

    verifier._then = at.world.features.reconcile
    again = await verifier.carry_out(specify)

    assert again.startswith(("refused", "finished"))
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING


async def test_a_replacement_a_decision_voided_is_proposed_again_by_the_verifier_and_approved_again(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.5: nobody but the Verifier's worker puts the two contracts before the
    Product Owner again, and nothing is built in between."""
    at = delivering_world
    quarter = {
        **STANDARD_RATE_CONTRACT,
        "contract_id": "B2-quarter",
        "expect_stdout": "125\n",
        "baseline_stdout": "120\n",
    }
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="the rate is a quarter",
        demonstrates=["AC1"],
        contract=quarter,
        replaces="B1",
        replaces_because="the Product Owner decided the rate is 25%",
    )
    still = delivering(demonstrated=False, replaces="B1", replaces_because="it is still 25%")
    applies_a_quarter = "import sys\nprint(int(sys.argv[1]) * 125 // 100)\n"
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, DEMONSTRATED, b2, still, DEMONSTRATED],
        verifier__You_are_asked_to_vouch=[VOUCHED, VOUCHED],
        verifier__You_are_asked_whether=[delivering(verdict="to_be_replaced"), STANDS],
        analyst__You_are_asked_to_assess=[VOUCHED, VOUCHED],
        builder__You_are_the_Builder=[
            builds({"vat.py": VAT_APPLIES_STANDARD_RATE}),
            builds({"vat.py": applies_a_quarter}),
        ],
        qa__You_are_reviewing=[NOTHING_FOUND, NOTHING_FOUND],
        sentinel__You_are_reviewing=[NOTHING_FOUND, NOTHING_FOUND],
    )
    await at.work()
    await at.world.answer(await at.world.ask(), "the rate is 25%")
    await at.work()
    await at.world.decide(await at.world.gate())  # approved; its red is observed
    assert await at.world.state("B2") is BehaviourState.IMPLEMENTING

    await at.world.answer(await at.world.ask(), "and rounded down")  # which ends the approval
    await at.work()

    assert await at.world.column("SELECT state FROM supersessions ORDER BY proposed_at") == [
        "void",
        "proposed",
    ]
    assert (
        "was approved by the Product Owner to replace what B1 requires"
        in (briefings(roles["verifier"], "You decide how")[3])
    )
    assert len(briefings(roles["builder"], "You are the Builder")) == 1  # nothing built for B2
    [build] = await at.world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'build' AND state = 'wanted'"
    )
    with pytest.raises(ActionRefused, match="B2 is to replace something on the trunk"):
        await at.world.claims.claim(BUILDER, build)  # type: ignore[arg-type]
    [again] = await at.world.stories.inbox(PROJECT)
    assert again.manifest["reason"] == "it is still 25%"

    await at.world.decide(again.gate_id)
    await at.work()

    on_trunk = at.world.git("show", f"{at.world.trunk()}:vat.py", at=at.world.remote)
    assert on_trunk == applies_a_quarter.strip()
    assert (await at.world.stories.inbox(PROJECT))[0].kind == "story"


async def test_a_replacement_its_verifier_withdraws_lets_its_red_be_observed_with_nothing_else_done(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.10: withdrawal wakes the control plane."""
    at = delivering_world
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="another",
        demonstrates=["AC1"],
        contract={
            **STANDARD_RATE_CONTRACT,
            "contract_id": "B2-quarter",
            "expect_stdout": "125\n",
            "baseline_stdout": "120\n",
        },
        replaces="B1",
        replaces_because="the rate is 25%",
    )
    await released(at, verifier__You_decide_how_a_user_story_is_shown=[B1, b2])
    await at.work()
    assert await at.world.state("B2") is BehaviourState.VALIDATING_RED
    supersessions = Supersessions(at.world.database, then=at.world.features.reconcile)

    await supersessions.withdraw(VERIFIER, PROJECT, await at.world.gate())

    assert await at.world.state("B2") is BehaviourState.IMPLEMENTING


async def test_a_voided_replacement_its_verifier_does_not_propose_again_is_asked_about_once(
    delivering_world: Delivering,
) -> None:
    """Answered by the contract written again and no replacement named: the behaviour
    goes on, and the Verifier is not asked the same thing by every pass."""
    at = delivering_world
    quarter = {
        **STANDARD_RATE_CONTRACT,
        "contract_id": "B2-quarter",
        "expect_stdout": "125\n",
        "baseline_stdout": "120\n",
    }
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="another",
        demonstrates=["AC1"],
        contract=quarter,
        replaces="B1",
        replaces_because="the rate is 25%",
    )
    no_longer = delivering(demonstrated=False, contract={**quarter, "contract_id": "B2-again"})
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, b2, no_longer],
        verifier__You_are_asked_whether=[STANDS, STANDS],
    )
    await at.work()
    assert await at.world.state("B2") is BehaviourState.VALIDATING_RED
    at.workers.pop("builder")  # B1 is on the trunk; nothing more is to be built here

    await at.world.answer(await at.world.ask(), "the rate stays as it is")
    await at.work()
    await at.world.features.reconcile(PROJECT)

    assert await at.world.column("SELECT state FROM supersessions") == ["void"]
    assert len(briefings(roles["verifier"], "was approved by the Product Owner")) == 1
    assert await at.world.column(
        "SELECT contract->>'contract_id' FROM behaviours WHERE behaviour_id = 'B2'"
    ) == ["B2-again"]
    assert await at.world.state("B2") is BehaviourState.IMPLEMENTING


async def test_a_seal_owed_before_a_decision_undid_what_it_was_of_is_not_claimed(
    delivering_world: Delivering,
) -> None:
    """3B-S3-2.4: every owner has reconfirmed, and the story has not been said to be
    demonstrated as it now stands."""
    at = delivering_world
    await released(at)
    verifier = at.workers.pop("verifier")
    for kind in ("specify", "specify"):  # B1, then "demonstrated": the seal is owed
        at.workers["verifier"] = verifier
        await verifier.carry_out((await at.world.claims.of(await _the(at, kind))) or {})
        at.workers.pop("verifier")
        await at.work()
    seal = await _the(at, "seal")

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.world.reconfirm_everything()

    with pytest.raises(ActionRefused, match="has not been said to be demonstrated as it now"):
        await at.world.claims.claim(VERIFIER, seal)


async def test_a_revision_of_the_roadmap_in_force_is_owed_once(
    delivering_world: Delivering,
) -> None:
    world = delivering_world.world
    await world.decide(await world.roadmap(STANDARD_RATE))

    async with world.database.connection() as connection, connection.transaction():
        await owe_a_revision(connection, PROJECT, "F1", "it no longer stands")
        await owe_a_revision(connection, PROJECT, "F1", "it still does not")

    assert await world.column(
        "SELECT issued_on->>'reason' FROM assignments WHERE kind = 'revise'"
    ) == ["it no longer stands"]


async def test_a_reply_to_an_invalid_red_that_only_names_a_replacement_rewrites_nothing(
    delivering_world: Delivering,
) -> None:
    """3B-S3-3.3: what is owed is the contract, written again."""
    at = delivering_world
    passes_already = delivering(**{**B1["delivered"], "contract": ALREADY_PASSES})
    only_replaces = delivering(demonstrated=False, replaces="B1", replaces_because="x")
    rewritten = delivering(demonstrated=False, contract=STANDARD_RATE_CONTRACT)
    roles = await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[passes_already, only_replaces, rewritten],
    )
    at.workers.pop("builder")

    await at.work()

    assert (
        "what is asked for is the contract, written again"
        in (briefings(roles["verifier"], "You decide how")[2])
    )
    assert await at.world.column("SELECT count(*) FROM supersessions") == [0]
    assert await at.world.state("B1") is BehaviourState.IMPLEMENTING


async def test_a_verifier_may_say_a_voided_replacement_is_not_proposed_again_and_the_build_goes_on(
    delivering_world: Delivering,
) -> None:
    """3B-S3-3.5: past its red, with no contract to write again."""
    at = delivering_world
    quarter = {
        **STANDARD_RATE_CONTRACT,
        "contract_id": "B2-quarter",
        "expect_stdout": "125\n",
        "baseline_stdout": "120\n",
    }
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="the rate is a quarter",
        demonstrates=["AC1"],
        contract=quarter,
        replaces="B1",
        replaces_because="the rate is 25%",
    )
    declines = delivering(demonstrated=False, no_longer_replaces=True)
    await released(
        at,
        verifier__You_decide_how_a_user_story_is_shown=[B1, b2, declines],
        verifier__You_are_asked_whether=[STANDS, STANDS],
    )
    await at.work()
    await at.world.decide(await at.world.gate())  # approved; its red is observed
    assert await at.world.state("B2") is BehaviourState.IMPLEMENTING
    at.workers.pop("builder")

    await at.world.answer(await at.world.ask(), "the rate stays as it is")
    await at.work()

    assert await at.world.column("SELECT state FROM supersessions") == ["void"]
    assert await at.world.column("SELECT state FROM obligations WHERE behaviour_id = 'B1'") == [
        "active"
    ]
    [build] = await at.world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'build' AND state = 'wanted'"
    )
    await at.world.claims.claim(BUILDER, build)  # type: ignore[arg-type]


async def test_work_refused_for_a_reason_that_still_holds_does_not_wake_the_control_plane(
    delivering_world: Delivering,
) -> None:
    """3B-S3-3.7: a pass would announce it again, to be refused again, while the gate
    waits for a person."""
    at = delivering_world
    await released(at)
    await at.work()  # the story is at its gate; everything is done
    await at.world.column(
        "UPDATE assignments SET state = 'wanted', grant_id = NULL, granted_to = NULL "
        "WHERE kind = 'assess' RETURNING 1"
    )
    await at.world.column("UPDATE stories SET seal_id = NULL RETURNING 1")  # so it is refused
    assess = (await at.world.claims.of(await _the(at, "assess"))) or {}
    passes: list[str] = []

    async def counting(project_id: str) -> None:
        passes.append(project_id)

    analyst = at.workers["analyst"]
    analyst._then = counting

    for _ in range(3):
        assert (await analyst.carry_out(assess)).startswith("refused: S1 has no seal")

    assert passes == []


async def test_a_planned_story_a_decision_changed_is_reworded_by_the_analyst(
    delivering_world: Delivering,
) -> None:
    """3B-S3-3.4: from an answer about that story, through the Analyst's verdict and a
    worker's revision, to the approval of the reworded story."""
    at, world = delivering_world, delivering_world.world
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    reworded = {
        "change": "reworded",
        "story_id": "S2",
        "title": "Greeting in French",
        "outcome": "the shopper is greeted in French",
        "criteria": [{"id": "AC1", "text": "bonjour is printed"}],
    }
    roles = staffed(
        analyst__You_propose_the_roadmap=[
            proposing({"change": "kept", "story_id": "S1"}, reworded)
        ],
        analyst__You_are_asked_whether=[
            delivering(verdict="to_be_replaced", because="the greeting is to be in French")
        ],
    )
    await at.staff(analyst=roles["analyst"])

    await world.answer(await world.ask(story="S2"), "greet in French")
    await at.work()

    [asked] = briefings(roles["analyst"], "You propose the roadmap")
    assert "story S2 no longer stands under the decisions now in force" in asked
    [proposal] = [each for each in await world.stories.inbox(PROJECT) if each.kind == "roadmap"]
    await world.decide(proposal.gate_id)
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == [
        "Greeting in French"
    ]


async def test_a_withdrawal_sent_again_wakes_the_control_plane_again_and_withdraws_nothing_more(
    delivering_world: Delivering,
) -> None:
    """3B-S3-3.11: cut short between the commit and the wake-up, and sent again."""
    at = delivering_world
    b2 = delivering(
        demonstrated=False,
        behaviour_id="B2",
        summary="another",
        demonstrates=["AC1"],
        contract={
            **STANDARD_RATE_CONTRACT,
            "contract_id": "B2-quarter",
            "expect_stdout": "125\n",
            "baseline_stdout": "120\n",
        },
        replaces="B1",
        replaces_because="the rate is 25%",
    )
    await released(at, verifier__You_decide_how_a_user_story_is_shown=[B1, b2])
    await at.work()
    gate = await at.world.gate()
    await Supersessions(at.world.database).withdraw(VERIFIER, PROJECT, gate)  # and no wake-up
    assert await at.world.state("B2") is BehaviourState.VALIDATING_RED

    again = Supersessions(at.world.database, then=at.world.features.reconcile)
    await again.withdraw(VERIFIER, PROJECT, gate)

    assert await at.world.state("B2") is BehaviourState.IMPLEMENTING
    assert await at.world.column("SELECT state FROM supersessions") == ["withdrawn"]


async def test_a_contract_restated_beside_stands_is_the_one_that_stands_or_the_reply_is_refused(
    delivering_world: Delivering,
) -> None:
    """What the real provider does: it fills in the contract when it says "stands". The
    same contract is no amendment; a different one is not what "stands" says."""
    at = delivering_world
    another = {**STANDARD_RATE_CONTRACT, "contract_id": "B1-another"}
    roles = await released(
        at,
        verifier__You_are_asked_whether=[
            delivering(verdict="stands", contract=another),
            delivering(verdict="stands", contract=STANDARD_RATE_CONTRACT),
        ],
    )
    at.workers.pop("builder")
    await at.work()

    await at.world.answer(await at.world.ask(), "zero-rated")
    await at.work()

    asked = briefings(roles["verifier"], "You are asked whether")
    assert 'if it is to change, the verdict is "amended"' in asked[1]
    assert await at.world.column("SELECT contract->>'contract_id', red_cycle FROM behaviours") == [
        "B1-standard-rate"
    ]
    assert await at.world.column(
        "SELECT v.state FROM invocations v JOIN assignments a USING (assignment_id) "
        "WHERE a.kind = 'reconfirm' AND v.role = 'verifier' ORDER BY v.attempt"
    ) == ["rejected", "applied"]


async def test_a_roadmap_or_a_story_is_reconfirmed_on_a_schema_that_offers_no_contract(
    delivering_world: Delivering,
) -> None:
    at = delivering_world
    roles = staffed()
    await at.staff(analyst=roles["analyst"])
    await at.world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await at.work()
    await at.world.answer(await at.world.ask(story=None), "prices are in euros")
    await at.work()

    [request] = [
        each for each in roles["analyst"].asked if "You are asked whether" in each.briefing
    ]
    offered = request.schema["properties"]["delivered"]["properties"]
    assert sorted(offered) == ["because", "verdict"]
