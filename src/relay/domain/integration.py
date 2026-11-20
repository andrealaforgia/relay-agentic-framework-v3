"""Rules for moving trunk: what a candidate is checked against, and what a push that did
not plainly succeed leaves behind.

A push can be accepted, refused, or simply never answered. An unanswered push may still
land later, so nothing here treats silence as failure.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from enum import StrEnum

from relay.domain.evidence import CheckContract


def regression_digest(policy_version: int, checks: Collection[CheckContract]) -> str:
    """Identifies exactly which checks a batch ran, so a batch cannot authorise an
    integration after the set it answered has changed."""
    members = sorted(check.digest for check in checks)
    canonical = json.dumps({"policy_version": policy_version, "members": members})
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def a_rejection_settles_the_intent(*, sends_started: int, sends_resolved: int) -> bool:
    """A refusal answers one send. The intent is settled by it only if no other send is
    still unanswered: one that is could yet be accepted and move trunk."""
    return sends_started - sends_resolved == 1


class TrunkReading(StrEnum):
    CONVERGED = "converged"
    UNDECIDED = "undecided"
    CONFLICT = "conflict"


def read_trunk(
    trunk: str, *, expected: str, candidate: str, contains_candidate: bool
) -> TrunkReading:
    """What the trunk head says about an intent whose push has no known outcome."""
    if trunk == candidate or contains_candidate:
        return TrunkReading.CONVERGED
    if trunk == expected:
        # The push may be lost, or may still be on its way.
        return TrunkReading.UNDECIDED
    return TrunkReading.CONFLICT
