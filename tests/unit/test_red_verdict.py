"""A red state counts only when the registered test fails for the registered reason."""

import pytest

from relay.domain.evidence import (
    Outcome,
    RedExpectation,
    RedRejection,
    TestObservation,
    judge_red,
)

EXPECTATION = RedExpectation(
    test_id="tests/acceptance/test_vat.py::test_standard_rate_is_applied",
    failure_contains="assert 100 == 120",
)


def observed(outcome: Outcome, failure_text: str = "") -> TestObservation:
    return TestObservation(
        test_id=EXPECTATION.test_id, outcome=outcome, failure_text=failure_text
    )


def test_the_registered_assertion_failing_is_a_valid_red() -> None:
    verdict = judge_red(EXPECTATION, [observed(Outcome.FAILED, "E  assert 100 == 120")])

    assert verdict.is_valid
    assert verdict.rejection is None


@pytest.mark.parametrize(
    ("outcome", "rejection"),
    [
        (Outcome.PASSED, RedRejection.ALREADY_PASSES),
        (Outcome.ERRORED, RedRejection.ERRORED_BEFORE_ASSERTING),
        (Outcome.SKIPPED, RedRejection.SKIPPED),
        (Outcome.TIMED_OUT, RedRejection.TIMED_OUT),
    ],
)
def test_anything_other_than_an_assertion_failure_is_not_red(
    outcome: Outcome, rejection: RedRejection
) -> None:
    verdict = judge_red(EXPECTATION, [observed(outcome, "assert 100 == 120")])

    assert not verdict.is_valid
    assert verdict.rejection is rejection


def test_a_test_that_never_ran_is_not_red() -> None:
    other = TestObservation(test_id="tests/test_other.py::test_x", outcome=Outcome.FAILED,
                            failure_text="assert 100 == 120")

    verdict = judge_red(EXPECTATION, [other])

    assert verdict.rejection is RedRejection.NOT_EXECUTED


def test_failing_for_another_reason_is_not_red() -> None:
    verdict = judge_red(
        EXPECTATION, [observed(Outcome.FAILED, "AttributeError: no attribute 'vat'")]
    )

    assert verdict.rejection is RedRejection.UNEXPECTED_FAILURE


def test_an_expectation_that_would_match_any_failure_is_refused() -> None:
    with pytest.raises(ValueError, match="failure_contains"):
        RedExpectation(test_id=EXPECTATION.test_id, failure_contains="  ")


def test_a_rejection_explains_itself() -> None:
    verdict = judge_red(EXPECTATION, [observed(Outcome.ERRORED, "SyntaxError")])

    assert EXPECTATION.test_id in verdict.reason
    assert "errored" in verdict.reason
