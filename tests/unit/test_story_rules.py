"""Stories above behaviours, and the Product Owner's gate above stories."""

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from relay.application.stories import StoryDraft
from relay.domain.story import (
    ActionRefused,
    Candidate,
    HumanAction,
    Principal,
    PrincipalKind,
    Receipt,
    SealRefused,
    StoryState,
    answer_again,
    next_story_to_release,
    permits_behaviour_work,
    require_human,
    require_sealable,
    require_well_formed,
)

ANDREA = Principal(PrincipalKind.HUMAN, "andrea")
BUILDER = Principal(PrincipalKind.WORKER, "worker-1", role="builder")


def test_a_product_owner_operation_needs_a_human() -> None:
    assert require_human(ANDREA) is ANDREA


@pytest.mark.parametrize(
    "principal", [BUILDER, Principal(PrincipalKind.CONTROL_PLANE, "relay")], ids=["worker", "relay"]
)
def test_nothing_but_a_human_may_act_as_the_product_owner(principal: Principal) -> None:
    with pytest.raises(ActionRefused, match="human"):
        require_human(principal)


@pytest.mark.parametrize("state", [StoryState.RELEASED, StoryState.CHANGES_REQUESTED])
def test_a_story_being_worked_on_permits_behaviour_work(state: StoryState) -> None:
    assert permits_behaviour_work(state)


@pytest.mark.parametrize(
    "state",
    [StoryState.PLANNED, StoryState.AWAITING_APPROVAL, StoryState.APPROVED, StoryState.REJECTED],
)
def test_a_story_not_being_worked_on_permits_none(state: StoryState) -> None:
    assert not permits_behaviour_work(state)


def test_a_list_that_covers_every_criterion_can_be_sealed() -> None:
    require_sealable({"AC1", "AC2"}, {"B1": {"AC1"}, "B2": {"AC1", "AC2"}})


def test_an_empty_list_cannot_be_sealed() -> None:
    with pytest.raises(SealRefused, match="no behaviours"):
        require_sealable({"AC1"}, {})


def test_a_list_that_leaves_a_criterion_out_cannot_be_sealed() -> None:
    with pytest.raises(SealRefused, match="AC2"):
        require_sealable({"AC1", "AC2"}, {"B1": {"AC1"}})


def test_a_behaviour_cannot_claim_a_criterion_the_story_does_not_have() -> None:
    with pytest.raises(SealRefused, match="AC9"):
        require_sealable({"AC1"}, {"B1": {"AC1", "AC9"}})


def test_a_behaviour_that_demonstrates_nothing_cannot_be_sealed() -> None:
    with pytest.raises(SealRefused, match="B2"):
        require_sealable({"AC1"}, {"B1": {"AC1"}, "B2": set()})


def roadmap(*states: StoryState, feature: str = "F1") -> list[Candidate]:
    return [Candidate(feature, f"S{n}", state) for n, state in enumerate(states, start=1)]


def test_the_first_planned_story_is_released_when_the_slot_is_free() -> None:
    chosen = next_story_to_release([roadmap(StoryState.PLANNED, StoryState.PLANNED)])

    assert chosen == Candidate("F1", "S1", StoryState.PLANNED)


def test_a_story_waits_for_every_earlier_one_to_be_approved() -> None:
    chosen = next_story_to_release([roadmap(StoryState.APPROVED, StoryState.PLANNED)])

    assert chosen is not None and chosen.story_id == "S2"


@pytest.mark.parametrize(
    "occupying",
    [StoryState.RELEASED, StoryState.CHANGES_REQUESTED, StoryState.AWAITING_APPROVAL],
)
def test_nothing_is_released_while_any_story_of_the_project_holds_the_slot(
    occupying: StoryState,
) -> None:
    features = [roadmap(occupying, StoryState.PLANNED), roadmap(StoryState.PLANNED, feature="F2")]

    assert next_story_to_release(features) is None


def test_a_rejected_story_keeps_its_successors_waiting() -> None:
    assert next_story_to_release([roadmap(StoryState.REJECTED, StoryState.PLANNED)]) is None


def test_the_feature_opened_first_goes_first_and_the_other_gets_its_turn() -> None:
    first = roadmap(StoryState.PLANNED)
    second = roadmap(StoryState.PLANNED, feature="F2")

    assert next_story_to_release([first, second]) == first[0]
    assert next_story_to_release([roadmap(StoryState.APPROVED), second]) == second[0]


APPROVE_G1 = HumanAction(ANDREA, "request-1", "G1", "approve", {})
RECEIPT = Receipt("effective", "approved")


def test_a_request_never_seen_is_not_answered_from_the_record() -> None:
    assert answer_again(APPROVE_G1, None) is None


def test_the_same_request_again_gets_the_receipt_it_already_has() -> None:
    assert answer_again(APPROVE_G1, (APPROVE_G1.digest, RECEIPT)) == RECEIPT


@pytest.mark.parametrize(
    "reused",
    [
        HumanAction(ANDREA, "request-1", "G2", "approve", {}),
        HumanAction(ANDREA, "request-1", "G1", "request_changes", {"reason": "not yet"}),
        HumanAction(ANDREA, "request-1", "G1", "approve", {"note": "different"}),
    ],
    ids=["another gate", "another action", "another payload"],
)
def test_a_request_id_reused_for_something_else_is_a_conflict(reused: HumanAction) -> None:
    with pytest.raises(ActionRefused, match="already used"):
        answer_again(reused, (APPROVE_G1.digest, RECEIPT))


@pytest.mark.parametrize(
    "action",
    [
        HumanAction(ANDREA, "r", "G1", "approve", {}),
        HumanAction(ANDREA, "r", "G1", "request_changes", {"reason": "split it"}),
    ],
)
def test_an_action_saying_exactly_what_it_has_to_is_well_formed(action: HumanAction) -> None:
    require_well_formed(action)


@pytest.mark.parametrize(
    "action",
    [
        HumanAction(ANDREA, "r", "G1", "merge", {}),
        HumanAction(ANDREA, "r", "G1", "approve", {"manifest_digest": "x"}),
        HumanAction(ANDREA, "r", "G1", "request_changes", {}),
        HumanAction(ANDREA, "r", "G1", "request_changes", {"reason": ""}),
        HumanAction(ANDREA, "r", "G1", "request_changes", {"reason": 7}),
    ],
    ids=["unknown action", "extra field", "missing reason", "empty reason", "not text"],
)
def test_an_action_saying_anything_else_is_refused(action: HumanAction) -> None:
    with pytest.raises(ActionRefused):
        require_well_formed(action)


def test_what_an_action_carries_cannot_be_changed_after_it_is_made() -> None:
    said = {"reason": "split it"}
    action = HumanAction(ANDREA, "r", "G1", "request_changes", said)
    digest = action.digest

    said["reason"] = "something else"

    assert action.payload == {"reason": "split it"}
    assert action.digest == digest


class EmptiesItselfWhenItsItemsAreRead(Mapping[str, str]):
    """Has one criterion until someone asks for its items, and none after that."""

    def __init__(self) -> None:
        self._criteria = {"AC1": "120 for 100"}

    def __getitem__(self, key: str) -> str:
        return self._criteria[key]

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._criteria))

    def __len__(self) -> int:
        return len(self._criteria)

    def items(self) -> Any:
        shown = list(self._criteria.items())
        self._criteria.clear()
        return shown


def test_what_a_story_draft_checks_is_what_it_keeps() -> None:
    draft = StoryDraft("S1", "Standard rate", "a gross price", EmptiesItselfWhenItsItemsAreRead())

    assert dict(draft.criteria) == {"AC1": "120 for 100"}


def test_a_story_draft_keeps_its_criteria_whatever_happens_to_the_callers() -> None:
    criteria = {"AC1": "120 for 100"}
    draft = StoryDraft("S1", "Standard rate", "a gross price", criteria)

    criteria["AC2"] = "slipped in afterwards"

    assert dict(draft.criteria) == {"AC1": "120 for 100"}
