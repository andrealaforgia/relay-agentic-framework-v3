"""The behaviour lifecycle. Only this code moves a behaviour between states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from relay.domain.evidence import (
    Execution,
    ProcessContract,
    RedVerdict,
    judge_red,
)


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
    contract: ProcessContract | None = None

    @classmethod
    def propose(cls, behaviour_id: str, summary: str) -> Behaviour:
        return cls(behaviour_id=behaviour_id, summary=summary)

    def begin_test_design(self) -> None:
        self._require("begin_test_design", BehaviourState.PROPOSED)
        self.state = BehaviourState.TEST_DESIGN

    def register_acceptance_test(self, contract: ProcessContract) -> None:
        self._require("register_acceptance_test", BehaviourState.TEST_DESIGN)
        self.contract = contract
        self.state = BehaviourState.VALIDATING_RED

    def require_awaiting_red(self) -> ProcessContract:
        """Refuse a red run that has no work left to do, before anything is launched."""
        self._require("run_red", BehaviourState.VALIDATING_RED)
        assert self.contract is not None
        return self.contract

    def record_red_run(self, execution: Execution) -> RedVerdict:
        contract = self.require_awaiting_red()
        verdict = judge_red(contract, execution)
        if verdict.is_valid:
            self.state = BehaviourState.IMPLEMENTING
        return verdict

    def _require(self, attempted: str, *required: BehaviourState) -> None:
        if self.state not in required:
            raise TransitionRefused(attempted, self.state, required)
