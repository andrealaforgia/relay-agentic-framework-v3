"""Stories, and the gate at which the Product Owner accepts one.

A behaviour is a unit of evidence; a story is a unit of value. Nothing here asks a model
whether a story may proceed: release, sealing and the gate are rules over recorded facts,
and the gate itself is opened only for a person.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class PrincipalKind(StrEnum):
    HUMAN = "human"
    WORKER = "worker"
    CONTROL_PLANE = "control_plane"


@dataclass(frozen=True)
class Principal:
    """Who is calling. Established by the trusted boundary, never by what a caller says
    about itself in a payload."""

    kind: PrincipalKind
    identity: str
    role: str = ""


class ActionRefused(Exception):
    pass


def require_human(principal: Principal) -> Principal:
    """Approving, rejecting and answering are a person's. A worker runs a model, and a
    model's recommendation must never become the Product Owner's decision."""
    if principal.kind is not PrincipalKind.HUMAN:
        raise ActionRefused("only a human principal may act as the Product Owner")
    return principal


class StoryState(StrEnum):
    PLANNED = "planned"
    RELEASED = "released"
    CHANGES_REQUESTED = "changes_requested"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


_WORKING = frozenset({StoryState.RELEASED, StoryState.CHANGES_REQUESTED})
# A story at its gate is not being worked on, yet no other story may start.
HOLDING_THE_SLOT = _WORKING | {StoryState.AWAITING_APPROVAL}


def permits_behaviour_work(state: StoryState) -> bool:
    return state in _WORKING


class SealRefused(Exception):
    pass


def require_sealable(
    criteria: Collection[str], demonstrates: Mapping[str, Collection[str]]
) -> None:
    """A sealed list is the Verifier's claim that these behaviours, and no others, show
    the story's criteria. Code cannot judge the claim; it can refuse one that is empty or
    that leaves a criterion with nothing to show it."""
    if not demonstrates:
        raise SealRefused("a story with no behaviours demonstrates nothing")
    for behaviour_id, claimed in sorted(demonstrates.items()):
        if not claimed:
            raise SealRefused(f"{behaviour_id} demonstrates no acceptance criterion")
        unknown = sorted(set(claimed) - set(criteria))
        if unknown:
            raise SealRefused(f"{behaviour_id} names criteria the story does not have: {unknown}")
    covered = {criterion for claimed in demonstrates.values() for criterion in claimed}
    uncovered = sorted(set(criteria) - covered)
    if uncovered:
        raise SealRefused(f"no behaviour demonstrates {uncovered}")


@dataclass(frozen=True)
class Candidate:
    feature_id: str
    story_id: str
    state: StoryState


def next_story_to_release(features: Sequence[Sequence[Candidate]]) -> Candidate | None:
    """The one story of a project that may start now, if any. `features` are the
    project's features in the order they were opened, each with the stories of its
    approved roadmap in order.

    One story at a time per project. Within a feature a story waits for every earlier
    one to be approved; a feature waits its turn behind the features opened before it
    only for as long as they have something to release."""
    stories = [story for roadmap in features for story in roadmap]
    if any(story.state in HOLDING_THE_SLOT for story in stories):
        return None
    for roadmap in features:
        for story in roadmap:
            if story.state is StoryState.PLANNED:
                return story
            if story.state is not StoryState.APPROVED:
                break  # this feature is stuck behind an earlier story; try the next
    return None


@dataclass(frozen=True)
class Receipt:
    disposition: str
    outcome: str


@dataclass(frozen=True)
class HumanAction:
    """One thing a person asked for, on one exact gate."""

    principal: Principal
    request_id: str
    target: str
    action: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Taken once, here: what is validated, hashed, applied and recorded is one thing,
        # whatever the caller does to its own mapping afterwards.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            {"target": self.target, "action": self.action, "payload": dict(self.payload)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def answer_again(action: HumanAction, recorded: tuple[str, Receipt] | None) -> Receipt | None:
    """What a request that may have been seen before is owed. None: it is new. A receipt:
    it is an exact replay, and gets the receipt it already has. A request id that was
    used for anything else acknowledges nothing: the caller is told, and nothing changes."""
    if recorded is None:
        return None
    digest, receipt = recorded
    if digest != action.digest:
        raise ActionRefused(
            f"request id {action.request_id!r} was already used for a different action"
        )
    return receipt


_PAYLOAD_OF = {"approve": frozenset[str](), "request_changes": frozenset({"reason"})}


def require_well_formed(action: HumanAction) -> None:
    """An action at a gate says one of a few things, in exactly the fields that thing
    has. Nothing else a caller sends is carried anywhere."""
    if action.action not in _PAYLOAD_OF:
        raise ActionRefused(f"{action.action!r} is not something that can be done at a gate")
    if set(action.payload) != _PAYLOAD_OF[action.action]:
        expected = sorted(_PAYLOAD_OF[action.action]) or "nothing"
        raise ActionRefused(f"{action.action} carries {expected}, and only that")
    if any(not isinstance(value, str) or not value.strip() for value in action.payload.values()):
        raise ActionRefused(f"{action.action} needs a stated reason")
