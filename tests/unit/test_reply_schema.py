"""A reply is checked against the schema recorded with its call, before any of it is
read as anything. Asking the provider to enforce the schema is not the same as its
having done so."""

import pytest

from relay.application.tasks import BUILD_REPLY, REVIEW_REPLY
from relay.domain.schema import problem_with, require_checkable

FINDING = {"severity": "high", "claim": "c", "evidence": "e", "remediation": "r"}


def test_a_reply_in_the_form_asked_for_has_no_problem() -> None:
    assert problem_with({"findings": [FINDING], "carried": []}, REVIEW_REPLY) is None
    assert problem_with({"note": "done"}, BUILD_REPLY) is None


@pytest.mark.parametrize(
    ("reply", "says"),
    [
        ([], "not a object"),
        ({"findings": []}, "has no carried"),
        ({"findings": [], "carried": [], "extra": 1}, "extra, which was not asked for"),
        ({"findings": [{**FINDING, "claim": 17}], "carried": []}, "claim of item 1"),
        ({"findings": [{**FINDING, "evidence": None}], "carried": []}, "evidence of item 1"),
        ({"findings": [{**FINDING, "remediation": True}], "carried": []}, "remediation of item 1"),
        ({"findings": [{**FINDING, "severity": "enormous"}], "carried": []}, "not one of"),
        ({"findings": "none", "carried": []}, "findings of the reply is not a array"),
        (
            {"findings": [], "carried": [{"finding_id": "F-1", "verdict": "fixed"}]},
            "verdict of item 1",
        ),
    ],
)
def test_a_reply_that_is_not_in_that_form_is_told_where(reply: object, says: str) -> None:
    """3B-S2-1.11."""
    found = problem_with(reply, REVIEW_REPLY)

    assert found is not None and says in found


def test_a_note_with_anything_beside_it_is_not_a_build_reply() -> None:
    assert problem_with({"note": "done", "also": "this"}, BUILD_REPLY) is not None
    assert problem_with({"note": 5}, BUILD_REPLY) is not None


def test_true_is_not_a_number() -> None:
    assert problem_with(True, {"type": "integer"}) is not None
    assert problem_with(3, {"type": "integer"}) is None
    assert problem_with(True, {"type": "boolean"}) is None


def test_a_schema_relay_cannot_check_in_full_is_refused_and_not_half_checked() -> None:
    with pytest.raises(ValueError, match="pattern"):
        problem_with("x", {"type": "string", "pattern": "^a"})
    with pytest.raises(ValueError, match="null"):
        problem_with(None, {"type": "null"})


def test_a_schema_is_refused_whole_before_any_reply_is_paid_for() -> None:
    """3B-S2-2.6: what Relay does not check may be anywhere in it, and is found without
    a reply to walk through."""
    require_checkable(REVIEW_REPLY)
    require_checkable({"type": "object"})
    deep = {"type": "object", "properties": {"each": {"type": "array", "items": {"oneOf": []}}}}
    with pytest.raises(ValueError, match="oneOf"):
        require_checkable(deep)
    with pytest.raises(ValueError, match="null"):
        require_checkable({"type": "array", "items": {"type": "null"}})
