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


def a_refusal_settles_the_intent(*, sends_started: int, sends_answered: int) -> bool:
    """A refusal answers one send, or none if nothing was sent. The intent is settled by
    it only if every send that left has been answered: one that has not could yet be
    accepted and move trunk."""
    return sends_started == sends_answered


class TrunkReading(StrEnum):
    CONVERGED = "converged"
    UNDECIDED = "undecided"
    CONFLICT = "conflict"
    UNVERIFIABLE = "unverifiable"


def read_trunk(trunk: str, *, expected: str, candidate: str, beyond_expected: bool) -> TrunkReading:
    """What one reading of the trunk head says about an intent whose push has no known
    outcome. `beyond_expected` is whether that same head strictly descends from the
    expected one.

    Only the candidate itself is convergence: a later head can contain the candidate's
    commit and still have thrown its change away. Only a head beyond the expected one is
    a conflict, because only that makes every push sent under this intent unable to
    land. A head that is older or unrelated is a reading that cannot be trusted, and
    settles nothing."""
    if trunk == candidate:
        return TrunkReading.CONVERGED
    if trunk == expected:
        # The push may be lost, or may still be on its way.
        return TrunkReading.UNDECIDED
    if beyond_expected:
        return TrunkReading.CONFLICT
    return TrunkReading.UNVERIFIABLE
