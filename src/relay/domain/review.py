"""The review gate: what stands between a verified green and integration.

Reviewers raise findings; this code decides what those findings mean for the gate. A
finding is never "resolved" as a standing fact. The role that raised it confirms the fix
on a particular candidate, and that confirmation counts for that candidate only: a later
candidate that drops the fix is blocked again until the role looks at it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class FindingStatus(StrEnum):
    OPEN = "open"
    DISPUTED = "disputed"
    WITHDRAWN = "withdrawn"


class Carried(StrEnum):
    """What a reviewer says about one of its own earlier findings on a new candidate."""

    VERIFIED_FIXED = "verified_fixed"
    STILL_PRESENT = "still_present"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Finding:
    finding_id: str
    role: str
    severity: Severity
    status: FindingStatus = FindingStatus.OPEN


@dataclass(frozen=True)
class ReviewPolicy:
    """Frozen on a review cycle when it is created, so a later change of policy cannot
    quietly alter what an open cycle requires."""

    required_roles: frozenset[str]
    blocking_from: Severity = Severity.HIGH

    def __post_init__(self) -> None:
        if not self.required_roles:
            raise ValueError("a review policy requires at least one reviewer role")


class GateState(StrEnum):
    AWAITING_REVIEWS = "awaiting_reviews"
    SATISFIED = "satisfied"
    CHANGES_REQUESTED = "changes_requested"
    PARKED = "parked"


@dataclass(frozen=True)
class Gate:
    state: GateState
    awaiting: frozenset[str] = frozenset()
    blocking: tuple[str, ...] = ()
    reason: str = ""


class SubmissionRefused(Exception):
    pass


def judge_gate(
    policy: ReviewPolicy,
    submitted: Collection[str],
    findings: Collection[Finding],
    verified_fixed_on_this_candidate: Collection[str],
) -> Gate:
    """The gate for one candidate, from what has been submitted about that candidate."""
    blocking = [
        finding
        for finding in findings
        if finding.severity >= policy.blocking_from
        and finding.status is not FindingStatus.WITHDRAWN
        and finding.finding_id not in verified_fixed_on_this_candidate
    ]
    orphaned = sorted(f.finding_id for f in blocking if f.role not in policy.required_roles)
    if orphaned:
        return Gate(
            GateState.PARKED,
            blocking=tuple(orphaned),
            reason="a blocking finding belongs to a reviewer role this project no longer "
            "requires; only a recorded Product Owner decision can settle it",
        )
    awaiting = policy.required_roles - set(submitted)
    if awaiting:
        return Gate(GateState.AWAITING_REVIEWS, awaiting=frozenset(awaiting))
    if blocking:
        return Gate(
            GateState.CHANGES_REQUESTED, blocking=tuple(sorted(f.finding_id for f in blocking))
        )
    return Gate(GateState.SATISFIED)


def require_authority_over(
    role: str, carried: Mapping[str, Carried], findings: Collection[Finding]
) -> None:
    """A reviewer speaks only for findings its own role raised. The Builder, and every
    other reviewer, can dispute a finding but cannot settle it."""
    by_id = {finding.finding_id: finding for finding in findings}
    for finding_id in carried:
        finding = by_id.get(finding_id)
        if finding is None:
            raise SubmissionRefused(f"{finding_id} is not a finding on this behaviour")
        if finding.role != role:
            raise SubmissionRefused(
                f"{finding_id} was raised by {finding.role}; {role} cannot settle it"
            )
        if finding.status is FindingStatus.WITHDRAWN:
            raise SubmissionRefused(f"{finding_id} has been withdrawn")
