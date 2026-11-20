"""The review gate: unresolved blocking findings prevent completion (scenario 6), and a
fix counts only for the candidate on which the reviewer who raised the finding saw it."""

import pytest

from relay.domain.review import (
    Carried,
    Finding,
    FindingStatus,
    GateState,
    ReviewPolicy,
    Severity,
    SubmissionRefused,
    judge_gate,
    require_authority_over,
)

POLICY = ReviewPolicy(required_roles=frozenset({"qa", "sentinel"}))
INJECTION = Finding("F1", "sentinel", Severity.HIGH)
NAMING = Finding("F2", "qa", Severity.LOW)


def test_the_gate_waits_for_every_required_reviewer() -> None:
    gate = judge_gate(POLICY, submitted={"qa"}, findings=[], verified_fixed_on_this_candidate=[])

    assert gate.state is GateState.AWAITING_REVIEWS
    assert gate.awaiting == {"sentinel"}


def test_two_reviews_with_no_findings_satisfy_the_gate() -> None:
    gate = judge_gate(POLICY, {"qa", "sentinel"}, [], [])

    assert gate.state is GateState.SATISFIED


def test_a_blocking_finding_sends_the_work_back() -> None:
    gate = judge_gate(POLICY, {"qa", "sentinel"}, [INJECTION], [])

    assert gate.state is GateState.CHANGES_REQUESTED
    assert gate.blocking == ("F1",)


def test_findings_below_the_threshold_do_not_block() -> None:
    gate = judge_gate(POLICY, {"qa", "sentinel"}, [NAMING], [])

    assert gate.state is GateState.SATISFIED


def test_the_threshold_is_the_policys_not_a_constant() -> None:
    strict = ReviewPolicy(frozenset({"qa", "sentinel"}), blocking_from=Severity.LOW)

    assert judge_gate(strict, {"qa", "sentinel"}, [NAMING], []).state is GateState.CHANGES_REQUESTED


def test_a_disputed_finding_still_blocks() -> None:
    disputed = Finding("F1", "sentinel", Severity.CRITICAL, FindingStatus.DISPUTED)

    assert judge_gate(POLICY, {"qa", "sentinel"}, [disputed], []).state is (
        GateState.CHANGES_REQUESTED
    )


def test_a_withdrawn_finding_no_longer_blocks() -> None:
    withdrawn = Finding("F1", "sentinel", Severity.HIGH, FindingStatus.WITHDRAWN)

    assert judge_gate(POLICY, {"qa", "sentinel"}, [withdrawn], []).state is GateState.SATISFIED


def test_a_fix_counts_only_for_the_candidate_it_was_verified_on() -> None:
    """Three candidates, one finding. Sentinel verifies the fix on B. QA asks for more.
    C drops the fix. Sentinel's silence about C must not clear the gate."""
    on_a = judge_gate(POLICY, {"qa", "sentinel"}, [INJECTION], verified_fixed_on_this_candidate=[])
    on_b = judge_gate(
        POLICY, {"qa", "sentinel"}, [INJECTION], verified_fixed_on_this_candidate=["F1"]
    )
    on_c = judge_gate(POLICY, {"qa", "sentinel"}, [INJECTION], verified_fixed_on_this_candidate=[])

    assert on_a.state is GateState.CHANGES_REQUESTED
    assert on_b.state is GateState.SATISFIED
    assert on_c.state is GateState.CHANGES_REQUESTED
    assert on_c.blocking == ("F1",)


def test_a_blocker_whose_reviewer_role_was_removed_parks_the_behaviour() -> None:
    qa_only = ReviewPolicy(required_roles=frozenset({"qa"}))

    gate = judge_gate(qa_only, {"qa"}, [INJECTION], [])

    assert gate.state is GateState.PARKED
    assert gate.blocking == ("F1",)
    assert "Product Owner" in gate.reason


def test_a_parked_blocker_takes_precedence_over_missing_reviews() -> None:
    qa_only = ReviewPolicy(required_roles=frozenset({"qa"}))

    assert judge_gate(qa_only, set(), [INJECTION], []).state is GateState.PARKED


def test_a_policy_needs_at_least_one_reviewer() -> None:
    with pytest.raises(ValueError, match="at least one reviewer"):
        ReviewPolicy(required_roles=frozenset())


def test_a_reviewer_may_settle_its_own_findings() -> None:
    require_authority_over("sentinel", {"F1": Carried.VERIFIED_FIXED}, [INJECTION, NAMING])


@pytest.mark.parametrize("carried", list(Carried))
def test_no_one_else_may_settle_a_reviewers_finding(carried: Carried) -> None:
    for other in ("qa", "builder"):
        with pytest.raises(SubmissionRefused, match="raised by sentinel"):
            require_authority_over(other, {"F1": carried}, [INJECTION, NAMING])


def test_a_finding_from_another_behaviour_cannot_be_settled_here() -> None:
    with pytest.raises(SubmissionRefused, match="not a finding on this behaviour"):
        require_authority_over("sentinel", {"F9": Carried.VERIFIED_FIXED}, [INJECTION])


def test_a_withdrawn_finding_cannot_be_revived_by_a_verdict() -> None:
    withdrawn = Finding("F1", "sentinel", Severity.HIGH, FindingStatus.WITHDRAWN)

    with pytest.raises(SubmissionRefused, match="withdrawn"):
        require_authority_over("sentinel", {"F1": Carried.STILL_PRESENT}, [withdrawn])
