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
    CANCELLED = "cancelled"
    # Dropped from its roadmap before it ever started.
    WITHDRAWN = "withdrawn"


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
class Member:
    """A story as a roadmap sees it: its state, and the rejected story it corrects, if
    it is a correction. Which story corrects which is fixed when the correction is
    created and never changes."""

    story_id: str
    state: StoryState
    corrects: str | None = None


def is_resolved(story: Member, members: Sequence[Member]) -> bool:
    """A story is resolved when it is approved, or when it is rejected and the story
    that corrects it is resolved. A rejection stays a rejection: it is answered, by a
    chain of corrections that ends in an approval, never undone."""
    if story.state is StoryState.APPROVED:
        return True
    if story.state is not StoryState.REJECTED:
        return False
    return any(is_resolved(other, members) for other in members if other.corrects == story.story_id)


@dataclass(frozen=True)
class Entry:
    """One line of a proposed roadmap: a story of the roadmap being revised that is
    kept, reworded or dropped, or a new story, which may correct a rejected one."""

    story_id: str
    kind: str
    corrects: str | None = None


def why_not_a_revision(effective: Sequence[Member], entries: Sequence[Entry]) -> str:
    """Why these entries may not replace the effective roadmap, or an empty string if
    they may. Checked when a proposal is published and again when it is approved: the
    stories it is about can change state in between.

    What is finished or under way is kept as it is and in its order. Only a planned
    story may be reworded, moved or dropped. Every rejection that is not yet answered
    has exactly one correction, and no rejection ever has two."""
    listed = [entry.story_id for entry in entries]
    if not [entry for entry in entries if entry.kind != "dropped"]:
        return "a roadmap needs at least one story"
    for story_id in listed:
        if listed.count(story_id) > 1:
            return f"{story_id} appears twice"
    known = {member.story_id: member for member in effective}
    for entry in entries:
        if entry.kind == "new" and entry.story_id in known:
            return f"{entry.story_id} is already a story of this roadmap"
        if entry.kind != "new" and entry.story_id not in known:
            return f"{entry.story_id} is not a story of the roadmap being revised"
    unaccounted = sorted(set(known) - set(listed))
    if unaccounted:
        return f"{unaccounted} of the roadmap being revised are not accounted for"
    settled = [m.story_id for m in effective if m.state is not StoryState.PLANNED]
    for entry in entries:
        if entry.story_id in settled and entry.kind != "kept":
            state = known[entry.story_id].state.value.replace("_", " ")
            return f"{entry.story_id} is {state}: it can only be kept as it is"
    if [entry.story_id for entry in entries if entry.story_id in settled] != settled:
        return f"{settled} are finished or under way and keep their order"
    result = [
        Member(entry.story_id, StoryState.PLANNED, entry.corrects)
        if entry.kind == "new"
        else known[entry.story_id]
        for entry in entries
        if entry.kind != "dropped"
    ]
    rejected = [member for member in result if member.state is StoryState.REJECTED]
    for entry in entries:
        if entry.kind == "new" and entry.corrects not in (
            None,
            *(member.story_id for member in rejected),
        ):
            return (
                f"{entry.story_id} corrects {entry.corrects}, "
                "which is not a rejected story of this feature"
            )
    for story in rejected:
        corrections = [member for member in result if member.corrects == story.story_id]
        if len(corrections) > 1:
            return f"{story.story_id} would have two corrections"
        if not corrections and not is_resolved(story, result):
            return f"{story.story_id} is rejected and nothing in this roadmap corrects it"
    return ""


@dataclass(frozen=True)
class Candidate:
    feature_id: str
    story_id: str
    state: StoryState
    # Its feature is closed, a question is open over it, or its roadmap or its own text
    # is waiting to be reconfirmed after a decision: it may not start, and the stories
    # after it in its feature wait behind it.
    stopped: bool = False
    # The rejected story this one corrects, if it is a correction.
    corrects: str | None = None


def next_story_to_release(
    features: Sequence[Sequence[Candidate]], *, slot_taken: bool = False
) -> Candidate | None:
    """The one story of a project that may start now, if any. `features` are the
    project's features that are open to work, in the order they were opened, each with
    the stories of its effective roadmap in order. `slot_taken` says that some story of
    the project, in any feature, a paused one included, already holds its place.

    One story at a time per project. A feature waits its turn behind the features
    opened before it only for as long as they have something to release.

    Within a feature, a rejection that has not been answered stops all ordinary work:
    the only story that may start is the correction of an unanswered rejection, found
    by what it corrects and not by where it was put. Otherwise a story waits for every
    earlier one to be resolved."""
    stories = [story for roadmap in features for story in roadmap]
    if slot_taken or any(story.state in HOLDING_THE_SLOT for story in stories):
        return None
    for roadmap in features:
        members = [Member(story.story_id, story.state, story.corrects) for story in roadmap]
        unanswered = {
            member.story_id
            for member in members
            if member.state is StoryState.REJECTED and not is_resolved(member, members)
        }
        for story, member in zip(roadmap, members, strict=True):
            startable = story.state is StoryState.PLANNED and not story.stopped
            if unanswered:
                if startable and story.corrects in unanswered:
                    return story
                continue
            if startable:
                return story
            if not is_resolved(member, members):
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


AT_A_GATE = {
    "approve": frozenset[str](),
    "request_changes": frozenset({"reason"}),
    "reject": frozenset({"reason"}),
    # Only where a feature's final check failed: run the checks again.
    "retry": frozenset[str](),
}
# Setting aside a story that is being worked on. A gate is one fixed thing to decide on;
# a story at work is not, so the person says which revision, under which decisions.
ON_A_STORY = {"reject": frozenset({"reason", "revision", "basis"})}
ON_A_QUESTION = {"answer": frozenset({"text"})}
ON_A_DECISION = {"replace": frozenset({"text"})}
ON_A_FEATURE = {
    "pause": frozenset({"reason"}),
    "resume": frozenset[str](),
    "cancel": frozenset({"reason"}),
}


def require_well_formed(
    action: HumanAction, allowed: Mapping[str, frozenset[str]] = AT_A_GATE
) -> None:
    """An action says one of a few things, in exactly the fields that thing has. Nothing
    else a caller sends is carried anywhere."""
    if action.action not in allowed:
        raise ActionRefused(f"{action.action!r} is not something that can be done here")
    if set(action.payload) != allowed[action.action]:
        expected = sorted(allowed[action.action]) or "nothing"
        raise ActionRefused(f"{action.action} carries {expected}, and only that")
    if any(not isinstance(value, str) or not value.strip() for value in action.payload.values()):
        raise ActionRefused(f"{action.action} needs a stated reason")
