"""Deterministic judgement of executable evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


class RedRejection(StrEnum):
    NOT_EXECUTED = "not_executed"
    ALREADY_PASSES = "already_passes"
    ERRORED_BEFORE_ASSERTING = "errored_before_asserting"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    UNEXPECTED_FAILURE = "unexpected_failure"


@dataclass(frozen=True)
class RedExpectation:
    """What the Verifier registers before the test is run."""

    test_id: str
    failure_contains: str

    def __post_init__(self) -> None:
        if not self.failure_contains.strip():
            raise ValueError(
                "failure_contains must name the expected failure; blank matches anything"
            )


@dataclass(frozen=True)
class TestObservation:
    __test__ = False

    test_id: str
    outcome: Outcome
    failure_text: str = ""


@dataclass(frozen=True)
class RedVerdict:
    rejection: RedRejection | None
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


_NOT_AN_ASSERTION_FAILURE = {
    Outcome.PASSED: (RedRejection.ALREADY_PASSES, "already passes on the baseline"),
    Outcome.ERRORED: (
        RedRejection.ERRORED_BEFORE_ASSERTING,
        "errored before reaching its assertion",
    ),
    Outcome.SKIPPED: (RedRejection.SKIPPED, "was skipped"),
    Outcome.TIMED_OUT: (RedRejection.TIMED_OUT, "timed out"),
}


def judge_red(expectation: RedExpectation, observations: Sequence[TestObservation]) -> RedVerdict:
    observation = next((o for o in observations if o.test_id == expectation.test_id), None)
    if observation is None:
        return _rejected(RedRejection.NOT_EXECUTED, expectation, "was not executed")
    if observation.outcome in _NOT_AN_ASSERTION_FAILURE:
        rejection, explanation = _NOT_AN_ASSERTION_FAILURE[observation.outcome]
        return _rejected(rejection, expectation, explanation)
    if expectation.failure_contains not in observation.failure_text:
        return _rejected(
            RedRejection.UNEXPECTED_FAILURE,
            expectation,
            f"failed, but not with the registered failure {expectation.failure_contains!r}",
        )
    return RedVerdict(rejection=None, reason=f"{expectation.test_id} failed as registered")


def _rejected(rejection: RedRejection, expectation: RedExpectation, explanation: str) -> RedVerdict:
    return RedVerdict(rejection=rejection, reason=f"{expectation.test_id} {explanation}")
