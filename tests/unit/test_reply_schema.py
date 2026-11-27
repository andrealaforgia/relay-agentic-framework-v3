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


def test_a_reply_in_the_form_asked_for_has_no_problem() -> None:
    assert problem_with({"findings": [FINDING], "carried": []}, REVIEW_REPLY) is None
    assert problem_with({"note": "done"}, BUILD_REPLY) is None


@pytest.mark.parametrize(
    ("reply", "says"),
    [
        ([], "not a object"),
        (
            {"findings": [{"severity": "high", "claim": "c", "evidence": "e"}], "carried": []},
            "has no remediation",
        ),
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


def test_a_schema_for_the_extra_properties_is_one_relay_does_not_check() -> None:
    """Astra's reservation in round three: only the yes-or-no form is understood."""
    with pytest.raises(ValueError, match="additionalProperties"):
        require_checkable({"type": "object", "additionalProperties": {"type": "integer"}})
    require_checkable({"type": "object", "additionalProperties": True})


QUESTION = {"text": "Refuse negatives?", "why": "no criterion says", "options": ["yes", "no"]}


@pytest.mark.parametrize(
    ("task", "schema", "delivered"),
    [
        (ReviewTask, REVIEW_REPLY, {"findings": [FINDING], "carried": []}),
        (BuildTask, BUILD_REPLY, {"note": "done"}),
    ],
)
def test_a_build_or_a_review_replies_in_one_form_and_the_schema_offers_no_other(
    task: Any, schema: dict[str, Any], delivered: dict[str, Any]
) -> None:
    """Offered two forms, a real model filled in both at once (the smoke against the
    provider, three paid builds refused). The schema it is given now has the one."""
    for reply in (delivered, {**delivered, "assumptions": ["x"]}, {"question": QUESTION}):
        assert problem_with(reply, schema) is None
        assert task.problem_with(None, reply) is None
    wrapped = {"outcome": "delivered", "assumptions": [], "delivered": delivered}
    for reply in (wrapped, {**wrapped, **delivered}, {"outcome": "question", "question": QUESTION}):
        assert "which was not asked for" in str(problem_with(reply, schema))
        assert task.problem_with(None, reply) is not None


@pytest.mark.parametrize(
    ("reply", "says"),
    [
        ({"note": "done", "assumptions": [" "]}, "an assumption says nothing"),
        ({"note": "done", "question": QUESTION}, "no note of work done"),
    ],
)
def test_what_a_schema_cannot_say_of_a_build_reply_its_task_says(
    reply: dict[str, Any], says: str
) -> None:
    """3B-S3-2.7 and 3B-S3-2.11."""
    assert problem_with(reply, BUILD_REPLY) is None  # the schema takes it
    found = BuildTask.problem_with(None, reply)  # type: ignore[arg-type]

    assert found is not None and says in found


def test_a_reply_its_task_would_not_take_is_applied_as_a_refusal_whoever_recorded_it() -> None:
    """3B-S3-2.11: read where it is applied, not only where a recorder chose to."""

    class Build:
        problem_with = BuildTask.problem_with

        def effects(self, invocation: Any) -> list[Any]:
            raise AssertionError("a reply that was refused has no effects of its own")

    reply = {"note": "done", "question": QUESTION}
    [effect] = Tasks({"build": Build()}).effects_for(
        {"kind": "build", "role": "builder", "reply": reply}
    )

    with pytest.raises(ActionRefused, match="no note of work done"):
        asyncio.run(effect.perform(None))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("task", "reply", "says"),
    [
        (
            ReviewTask,
            {
                "outcome": "delivered",
                "assumptions": [],
                "delivered": {"findings": "", "carried": {}},
            },
            "not asked for",
        ),
        (
            BuildTask,
            {"outcome": "delivered", "delivered": {"note": "done", "question": QUESTION}},
            "not asked for",
        ),
        (BuildTask, {"delivered": {"note": "done"}, "note": "also"}, "not asked for"),
        (BuildTask, {"outcome": "delivered", "delivered": "done"}, "not asked for"),
        (ReviewTask, {"delivered": {"findings": [], "carried": [], "extra": 1}}, "not asked for"),
    ],
)
def test_a_reply_in_the_other_form_or_in_both_has_no_effect_of_the_work(
    task: Any, reply: dict[str, Any], says: str
) -> None:
    """3B-S3-3.1: neither a review of nothing nor a build that also asks. The one form
    is the whole of what is accepted."""
    found = task.problem_with(None, reply)

    assert found is not None and says in found
    [effect] = Tasks({"work": task.__new__(task)}).effects_for(
        {"kind": "work", "role": "any", "reply": reply}
    )
    assert effect.name == "read"  # and so no effect of the work itself
