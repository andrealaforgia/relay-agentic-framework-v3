"""The behaviour lifecycle. Only this code moves a behaviour between states."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from relay.domain.evidence import RedExpectation, RedVerdict, TestObservation, judge_red


class BehaviourState(StrEnum):
    PROPOSED = "proposed"
    TEST_DESIGN = "test_design"
    VALIDATING_RED = "validating_red"
    IMPLEMENTING = "implementing"
    REVIEW = "review"
    INTEGRATED = "integrated"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class TransitionRefused(Exception):
    def __init__(
        self, attempted: str, current: BehaviourState, required: tuple[BehaviourState, ...]
    ) -> None:
        self.attempted = attempted
        self.current = current
        self.required = required
        allowed = " or ".join(state.value for state in required)
        super().__init__(f"cannot {attempted} while {current.value}; requires {allowed}")


@dataclass
class Behaviour:
    behaviour_id: str
    summary: str
    state: BehaviourState = BehaviourState.PROPOSED
    red_expectation: RedExpectation | None = None
    contract_digest: str | None = None

    @classmethod
    def propose(cls, behaviour_id: str, summary: str) -> Behaviour:
        return cls(behaviour_id=behaviour_id, summary=summary)

    def begin_test_design(self) -> None:
        self._require("begin_test_design", BehaviourState.PROPOSED)
        self.state = BehaviourState.TEST_DESIGN

    def register_acceptance_test(self, expectation: RedExpectation, contract_digest: str) -> None:
        self._require("register_acceptance_test", BehaviourState.TEST_DESIGN)
        self.red_expectation = expectation
        self.contract_digest = contract_digest
        self.state = BehaviourState.VALIDATING_RED

    def record_red_run(self, observations: Sequence[TestObservation]) -> RedVerdict:
        self._require("record_red_run", BehaviourState.VALIDATING_RED)
        assert self.red_expectation is not None
        verdict = judge_red(self.red_expectation, observations)
        if verdict.is_valid:
            self.state = BehaviourState.IMPLEMENTING
        return verdict

    def _require(self, attempted: str, *required: BehaviourState) -> None:
        if self.state not in required:
            raise TransitionRefused(attempted, self.state, required)
