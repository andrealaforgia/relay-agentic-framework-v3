"""A reply is checked against the schema recorded with its call, before any of it is
read as anything. Asking the provider to enforce the schema is not the same as its
having done so."""

import asyncio
from typing import Any

import pytest

from relay.application.tasks import BUILD_REPLY, REVIEW_REPLY, BuildTask, ReviewTask, Tasks
from relay.domain.schema import problem_with, require_checkable
from relay.domain.story import ActionRefused

FINDING = {"severity": "high", "claim": "c", "evidence": "e", "remediation": "r"}
QUESTION = {"text": "Refuse negatives?", "why": "no criterion says", "options": ["yes", "no"]}
NOTHING = {"findings": [], "carried": []}


def delivering(delivered: Any, **beside: Any) -> dict[str, Any]:
    return {"outcome": "delivered", "assumptions": [], "delivered": delivered, **beside}


ASKING = {"outcome": "question", "assumptions": [], "question": QUESTION}


def test_a_reply_in_the_form_asked_for_has_no_problem() -> None:
    assert problem_with(delivering({"findings": [FINDING], "carried": []}), REVIEW_REPLY) is None
    assert problem_with(delivering({"note": "done"}), BUILD_REPLY) is None
    assert problem_with(ASKING, REVIEW_REPLY) is None
    assert problem_with(ASKING, BUILD_REPLY) is None


@pytest.mark.parametrize(
    ("delivered", "says"),
    [
        ([], "delivered of the reply is not a object"),
        ({"findings": [{"severity": "high", "claim": "c", "evidence": "e"}]}, "has no remediation"),
        ({**NOTHING, "extra": 1}, "extra, which was not asked for"),
        ({"findings": [{**FINDING, "claim": 17}]}, "claim of item 1"),
        ({"findings": [{**FINDING, "evidence": None}]}, "evidence of item 1"),
        ({"findings": [{**FINDING, "remediation": True}]}, "remediation of item 1"),
        ({"findings": [{**FINDING, "severity": "enormous"}]}, "not one of"),
        ({"findings": "none", "carried": []}, "findings of delivered of the reply is not a array"),
        (
            {"findings": [], "carried": [{"finding_id": "F-1", "verdict": "fixed"}]},
            "verdict of item 1",
        ),
    ],
)
def test_a_reply_that_is_not_in_that_form_is_told_where(delivered: object, says: str) -> None:
    """3B-S2-1.11."""
    found = problem_with(delivering(delivered), REVIEW_REPLY)

    assert found is not None and says in found


def test_a_note_with_anything_beside_it_is_not_a_build_reply() -> None:
    found = problem_with(delivering({"note": "done", "commit": "abc"}), BUILD_REPLY)

    assert found is not None and "commit, which was not asked for" in found


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


def test_a_schema_for_the_extra_properties_is_one_relay_does_not_check() -> None:
    """Astra's reservation in round three: only the yes-or-no form is understood."""
    with pytest.raises(ValueError, match="additionalProperties"):
        require_checkable({"type": "object", "additionalProperties": {"type": "integer"}})
    require_checkable({"type": "object", "additionalProperties": True})


@pytest.mark.parametrize(
    ("task", "schema", "delivered"),
    [
        (ReviewTask, REVIEW_REPLY, {"findings": [FINDING], "carried": []}),
        (BuildTask, BUILD_REPLY, {"note": "done"}),
    ],
)
def test_a_build_or_a_review_is_offered_the_one_form_every_kind_of_work_replies_in(
    task: Any, schema: dict[str, Any], delivered: dict[str, Any]
) -> None:
    """3B-S3-2.12 and 3B-S4-1.6. Offered two forms, a real model filled in both: the
    schema of a new call has the common one alone. A reply recorded under the form these
    two had before is still read by the task, so that a result already bought is not
    made defective by the change; the schema of a new call would not take it."""
    for reply in (delivering(delivered), delivering(delivered, assumptions=["x"]), ASKING):
        assert problem_with(reply, schema) is None
        assert task.problem_with(None, reply) is None
    for earlier in (delivered, {**delivered, "assumptions": ["x"]}, {"question": QUESTION}):
        assert problem_with(earlier, schema) is not None  # not what a new call is offered
        assert task.problem_with(None, earlier) is None  # and still read, as it was bought


@pytest.mark.parametrize(
    ("task", "reply", "says"),
    [
        # A review of nothing, got by putting what is not a review under delivered.
        (ReviewTask, delivering({"findings": "", "carried": {}}), "is not a array"),
        # A build that also asks, one inside the other.
        (BuildTask, delivering({"note": "done", "question": QUESTION}), "not asked for"),
        # The two forms at once.
        (BuildTask, delivering({"note": "done"}, note="also"), "not asked for"),
        (BuildTask, {"note": "done", "outcome": "delivered", "assumptions": []}, "not asked for"),
        (BuildTask, delivering("done"), "is not a object"),
        (ReviewTask, delivering({**NOTHING, "extra": 1}), "not asked for"),
        # What the schema cannot say.
        (BuildTask, delivering({"note": "done"}, question=QUESTION), "no question"),
        (BuildTask, {**ASKING, "delivered": {"note": "x"}}, "delivers nothing"),
        (BuildTask, {"outcome": "question", "assumptions": []}, "has a question"),
        (BuildTask, {"outcome": "delivered", "assumptions": []}, "has what was asked for"),
        (BuildTask, delivering({"note": "done"}, assumptions=[" "]), "an assumption says nothing"),
        # The earlier form, held to the whole of what it was.
        (BuildTask, {"note": "done", "commit": "abc"}, "not asked for"),
        (ReviewTask, {"findings": "none", "carried": []}, "is not a array"),
        # The earlier form, mixed or blank.
        (BuildTask, {"note": "done", "question": QUESTION}, "no note of work done"),
        (BuildTask, {"note": "done", "assumptions": [" "]}, "an assumption says nothing"),
    ],
)
def test_a_reply_that_is_neither_form_whole_has_no_effect_of_the_work(
    task: Any, reply: dict[str, Any], says: str
) -> None:
    """3B-S3-2.7, 3B-S3-2.11 and 3B-S3-3.1: read where it is applied, whoever recorded
    it, and applied as a refusal."""
    found = task.problem_with(None, reply)

    assert found is not None and says in found, found
    [effect] = Tasks({"work": task.__new__(task)}).effects_for(
        {"kind": "work", "role": "any", "reply": reply}
    )
    assert effect.name == "read"
    with pytest.raises(ActionRefused):
        asyncio.run(effect.perform(None))  # type: ignore[arg-type]


def test_what_a_real_model_leaves_out_or_fills_in_harmlessly_is_not_a_paid_call_refused() -> None:
    """Each of these came back from the real provider and was refused, until it was
    plain that nothing in them was in doubt."""
    from relay.application.authoring import SpecifyTask

    # A reviewer with nothing to carry leaves the list out.
    assert ReviewTask.problem_with(None, delivering({"findings": []})) is None  # type: ignore[arg-type]
    # "Demonstrated", with the other fields of the schema filled in empty beside it.
    demonstrated = {"demonstrated": True, "summary": "", "demonstrates": []}
    assert SpecifyTask._problem_with_delivered(None, demonstrated) is None  # type: ignore[arg-type]
