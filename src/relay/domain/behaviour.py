"""The behaviour lifecycle. Only this code moves a behaviour between states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from relay.domain.evidence import (
    Execution,
    GreenRejection,
    GreenVerdict,
    PairedRed,
    ProcessContract,
    RedRejection,
    RedVerdict,
    judge_green,
    judge_red,
)
from relay.domain.identifiers import require_identifier


class BehaviourState(StrEnum):
    PROPOSED = "proposed"
    TEST_DESIGN = "test_design"
    VALIDATING_RED = "validating_red"
    IMPLEMENTING = "implementing"
    REVIEW = "review"
    INTEGRATED = "integrated"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_DECISION = "needs_decision"


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
    red_cycle: int = 0
    # A reviewed candidate lost its footing and the contract is being observed again.
    reopened_from_review: bool = False

    @classmethod
    def propose(cls, behaviour_id: str, summary: str) -> Behaviour:
        return cls(behaviour_id=require_identifier("behaviour", behaviour_id), summary=summary)

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

    def record_red_run(
        self, execution: Execution, *, started_in_cycle: int, withdrawn: bool = False
    ) -> RedVerdict:
        """Judge a red run that has finished. A run is admitted to start only while the
        behaviour awaits red; by the time it finishes the behaviour may have moved on, or
        its feature may have been cancelled (`withdrawn`), and then the run is recorded
        as superseded and changes nothing."""
        assert self.contract is not None
        moved_on = self.state is not BehaviourState.VALIDATING_RED
        if withdrawn or moved_on or started_in_cycle != self.red_cycle:
            return RedVerdict(
                RedRejection.SUPERSEDED,
                f"{self.contract.contract_id}: the behaviour moved on while this run was in flight",
            )
        verdict = judge_red(self.contract, execution)
        if verdict.is_valid:
            self.state = BehaviourState.IMPLEMENTING
            self.reopened_from_review = False
        elif self.reopened_from_review and verdict.rejection is RedRejection.ALREADY_PASSES:
            # Not a bad test: the behaviour arrived on trunk by another route while this
            # one was in review. Whether anything is left to build is not Relay's call.
            self.state = BehaviourState.NEEDS_DECISION
        return verdict

    def require_awaiting_green(self) -> ProcessContract:
        """Refuse a green run on a behaviour that is not being implemented."""
        self._require("run_green", BehaviourState.IMPLEMENTING)
        assert self.contract is not None
        return self.contract

    def record_green_run(
        self,
        execution: Execution,
        red_at_launch: PairedRed,
        red_now: PairedRed | None,
        *,
        candidate_descends_from_red: bool,
        still_owed: bool,
    ) -> GreenVerdict:
        """Judge a candidate against the red it was launched to answer, provided that red
        is still the one on record now and the build this run answers is still owed: a
        run that outlives its assignment is kept as evidence and changes nothing."""
        assert self.contract is not None
        contract = self.contract
        moved_on = self.state is not BehaviourState.IMPLEMENTING
        if moved_on or red_now != red_at_launch:
            return GreenVerdict(
                GreenRejection.RED_SUPERSEDED,
                f"{contract.contract_id}: the red this run answered is no longer the one on "
                "record, or the behaviour moved on while this run was in flight",
            )
        if not still_owed:
            return GreenVerdict(
                GreenRejection.ASSIGNMENT_SUPERSEDED,
                f"{contract.contract_id}: the work this run answered was replaced or "
                "withdrawn while it was in flight",
            )
        verdict = judge_green(
            contract,
            execution,
            red_at_launch,
            candidate_descends_from_red=candidate_descends_from_red,
        )
        if verdict.is_valid:
            self.state = BehaviourState.REVIEW
        return verdict

    def reopen_red(self) -> None:
        """Hand the behaviour back for a fresh red, when the one on record can no longer
        be paired (the runtime changed, say). The contract stays; the old pairing does not."""
        self._require("reopen_red", BehaviourState.IMPLEMENTING)
        self.state = BehaviourState.VALIDATING_RED
        self.red_cycle += 1

    def request_changes(self) -> None:
        """Reviewers found something that blocks: the work goes back to the Builder."""
        self._require("request_changes", BehaviourState.REVIEW)
        self.state = BehaviourState.IMPLEMENTING

    def park(self) -> None:
        """Nothing a Builder or reviewer can do will move this; a person must decide."""
        self._require("park", BehaviourState.REVIEW)
        self.state = BehaviourState.BLOCKED

    def reopen_from_review(self) -> None:
        """What the red and the green were observed on is no longer what integration would
        build on (trunk moved, or the runtime changed), so both must be observed again."""
        self._require("reopen_from_review", BehaviourState.REVIEW)
        self.state = BehaviourState.VALIDATING_RED
        self.red_cycle += 1
        self.reopened_from_review = True

    def integrate(self) -> None:
        """Trunk now holds the reviewed candidate."""
        self._require("integrate", BehaviourState.REVIEW)
        self.state = BehaviourState.INTEGRATED

    def retry_integration(self) -> None:
        """The repository refused the update; someone has dealt with why."""
        self._require("retry_integration", BehaviourState.BLOCKED)
        self.state = BehaviourState.REVIEW

    def _require(self, attempted: str, *required: BehaviourState) -> None:
        if self.state not in required:
            raise TransitionRefused(attempted, self.state, required)
