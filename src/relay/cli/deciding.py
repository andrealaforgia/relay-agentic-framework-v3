"""The Product Owner's decisions, as commands.

Each resolves what it is about from the record as it stands now, shows that on the
controlling terminal, reads the passphrase there, and only then asks the service, under
a request identity that was taken before anything was asked. The service's receipt is
what the command prints: the same request sent again is given the same receipt.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from relay.application.inbox import (
    FINAL_CHECK,
    GATE,
    PARKED_CALL,
    QUESTION,
    REFUSED_PUSH,
    Inbox,
    Item,
)
from relay.application.stories import closure_in, standing_of
from relay.domain.story import HumanAction, Principal, PrincipalKind, Receipt
from relay.runtime import Relay

PRODUCT_OWNER = Principal(PrincipalKind.HUMAN, "product-owner")

VERBS = (
    "approve",
    "request-changes",
    "reject",
    "retry",
    "set-aside",
    "answer",
    "replace-decision",
    "pause",
    "resume",
    "cancel",
    "start",
)


class CannotDecide(Exception):
    """There is nothing here that this can be done to, and it says why."""


@dataclass(frozen=True)
class Decision:
    """One thing a person is about to decide: what they are shown of it, what it is held
    to, and how it is carried out once they have confirmed."""

    verb: str
    # Shown on the terminal the passphrase is then read from.
    shown: str
    # What identifies exactly this decision: the same request sent again has the same.
    asked: str
    # A digest of the record as shown, for a decision prepared now and confirmed later.
    basis: str
    carry_out: Callable[[str], Awaitable[Receipt]]


def _evidence(item: Item) -> str:
    return json.dumps(item.evidence, indent=2, sort_keys=True, default=str)


async def resolve(
    relay: Relay,
    project_id: str,
    verb: str,
    target: str,
    text: str | None = None,
    reason: str | None = None,
) -> Decision:
    """What this verb on this target is, as the record has it now. Refuses what cannot be
    done to it, naming what can."""
    if verb == "start":
        problem = (text or "").strip()
        if not problem:
            raise CannotDecide("a feature starts from a stated problem")

        async def start(request_id: str) -> Receipt:
            return await relay.stories.start_feature(
                project_id,
                HumanAction(PRODUCT_OWNER, request_id, "", "start", {"problem": problem}),
            )

        return Decision(
            verb, f"Start a feature in {project_id} from this problem:\n\n  {problem}\n",
            f"{project_id}:start:{problem}", f"start:{problem}", start,
        )  # fmt: skip
    if verb in ("pause", "resume", "cancel"):
        return await _on_a_feature(relay, project_id, verb, target, reason)
    if verb == "replace-decision":
        return await _on_a_decision(relay, project_id, target, text)
    item = await Inbox(relay.pool).item(project_id, target)
    if item is None:
        if verb == "reject":
            return await _on_a_story_at_work(relay, project_id, target, reason)
        raise CannotDecide(
            f"nothing waiting in {project_id} is {target!r}: see `relay inbox` for what is"
        )
    allowed = item.actions
    if verb not in allowed:
        raise CannotDecide(
            f"{item.about} is not something to {verb}: it can be "
            f"{', '.join(allowed[:-1]) + ' or ' + allowed[-1] if len(allowed) > 1 else allowed[0]}"
        )
    needs_reason = verb in ("request-changes", "reject") or (
        verb == "retry" and item.kind == REFUSED_PUSH
    )
    if needs_reason and not (reason or "").strip():
        raise CannotDecide(f"to {verb} this, say why with --reason")
    if verb == "answer" and not (text or "").strip():
        raise CannotDecide("an answer says something")
    said = (text if verb == "answer" else reason) or ""
    shown = (
        f"In {project_id}, you are about to {verb}: {item.about}\n\n{_evidence(item)}\n"
        + (f"\nYou say: {said.strip()}\n" if said.strip() else "")
        + f"\n(item {item.item_id}, as shown: {item.basis})\n"
    )

    async def carry_out(request_id: str) -> Receipt:
        def by(action: str, **payload: str) -> HumanAction:
            return HumanAction(PRODUCT_OWNER, request_id, item.item_id, action, payload)

        if item.kind in (GATE, FINAL_CHECK):
            action = verb.replace("-", "_")
            return await relay.features.decide(by(action, **({"reason": said} if said else {})))
        if item.kind == QUESTION:
            return await relay.decisions.answer(by("answer", text=said))
        if item.kind == PARKED_CALL:
            return await relay.invocations.decide(by(verb.replace("-", "_")))
        assert item.kind == REFUSED_PUSH
        # The receipt is committed with the retry it allows: sent again, this request is
        # given it, though the item is by then no longer waiting.
        receipt = await relay.features.retry_integration(
            project_id,
            item.evidence["behaviour"],
            said,
            request_id,
            uuid.UUID(item.item_id),
            by("retry", reason=said),
        )
        assert receipt is not None
        return receipt

    return Decision(
        verb, shown, f"{project_id}:{verb}:{item.item_id}:{said}", item.basis, carry_out
    )


async def _on_a_feature(
    relay: Relay, project_id: str, verb: str, feature_id: str, reason: str | None
) -> Decision:
    async with relay.pool.connection() as connection:
        feature = await (
            await connection.execute(
                "SELECT feature_id, problem, state, closed_by FROM features "
                "WHERE project_id = %s AND feature_id = %s",
                (project_id, feature_id),
            )
        ).fetchone()
        closure = await closure_in(connection, project_id, feature_id)
    if feature is None:
        raise CannotDecide(f"there is no feature {feature_id!r} in {project_id}")
    standing = feature["closed_by"] or feature["state"]
    if verb != "resume" and not (reason or "").strip():
        raise CannotDecide(f"to {verb} a feature, say why with --reason")
    if verb == "resume" and closure is None:
        raise CannotDecide(
            f"feature {feature_id} is {standing}, not paused: there is nothing to resume"
        )
    shown = (
        f"In {project_id}, you are about to {verb} feature {feature_id} ({standing}):\n\n"
        f"  {feature['problem']}\n"
        + (f"\nYou say: {(reason or '').strip()}\n" if verb != "resume" else "")
        + (
            f"\n(the pause this lifts is entry {closure} of the ledger)\n"
            if verb == "resume"
            else ""
        )
    )

    basis = standing_of(feature, closure)
    # Held, in the transaction that decides, to the feature as the person is shown it
    # now: they may take their time over the passphrase, and it may move meanwhile.
    held_to = basis

    async def carry_out(request_id: str) -> Receipt:
        if verb == "resume":
            action = HumanAction(
                PRODUCT_OWNER,
                request_id,
                feature_id,
                "lift",
                {"closure": held_to.rsplit("/", 1)[-1], "as_seen": held_to},
            )
        else:
            action = HumanAction(
                PRODUCT_OWNER,
                request_id,
                feature_id,
                verb,
                {"reason": (reason or "").strip(), "as_seen": held_to},
            )
        return await relay.features.close(project_id, action)

    return Decision(
        verb, shown, f"{project_id}:{verb}:{feature_id}:{reason or ''}:{closure}", basis, carry_out
    )


async def _on_a_decision(relay: Relay, project_id: str, target: str, text: str | None) -> Decision:
    if not (text or "").strip():
        raise CannotDecide("a decision that replaces another says something")
    async with relay.pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT decision_id, feature_id, number, text FROM decisions d "
                "WHERE project_id = %s AND NOT EXISTS (SELECT 1 FROM decisions later "
                "WHERE later.replaces = d.decision_id)",
                (project_id,),
            )
        ).fetchall()
    matching = [
        row
        for row in rows
        if str(row["decision_id"]) == target
        or (len(target) >= 6 and str(row["decision_id"]).startswith(target))
        or f"{row['feature_id']}/D{row['number']}" == target
    ]
    if len(matching) != 1:
        raise CannotDecide(
            f"no one decision in force in {project_id} is {target!r}: name it by its id, "
            "or as FEATURE/Dnumber"
        )
    decision = matching[0]
    said = (text or "").strip()
    shown = (
        f"In {project_id}, you are about to replace decision {decision['number']} of "
        f"{decision['feature_id']}:\n\n  {decision['text']}\n\nwith:\n\n  {said}\n"
    )

    async def carry_out(request_id: str) -> Receipt:
        return await relay.decisions.replace(
            HumanAction(
                PRODUCT_OWNER, request_id, str(decision["decision_id"]), "replace", {"text": said}
            )
        )

    return Decision(
        "replace-decision", shown, f"{project_id}:replace:{decision['decision_id']}:{said}",
        f"decision:{decision['decision_id']}", carry_out,
    )  # fmt: skip


async def _on_a_story_at_work(
    relay: Relay, project_id: str, story_id: str, reason: str | None
) -> Decision:
    """Setting aside a story that is being worked on: not one fixed thing, as a gate is,
    so the decision names the revision of the story and the decisions it stood under, as
    the person is shown them."""
    if not (reason or "").strip():
        raise CannotDecide("to reject a story, say why with --reason")
    async with relay.pool.connection() as connection:
        story = await (
            await connection.execute(
                "SELECT story_id, feature_id, title, state, revision, "
                "story_basis(project_id, story_id) AS basis FROM stories "
                "WHERE project_id = %s AND story_id = %s",
                (project_id, story_id),
            )
        ).fetchone()
    if story is None:
        raise CannotDecide(
            f"nothing waiting in {project_id} is {story_id!r}, and there is no story of that "
            "name: see `relay inbox` and `relay status`"
        )
    said = (reason or "").strip()
    shown = (
        f"In {project_id}, you are about to reject story {story_id} of {story['feature_id']} "
        f"({story['state']}), which is being worked on:\n\n  {story['title']}\n\n"
        f"You say: {said}\n\n(as it stands at revision {story['revision']}, under decision "
        f"basis {story['basis']})\n"
    )

    async def carry_out(request_id: str) -> Receipt:
        return await relay.stories.reject(
            project_id,
            HumanAction(
                PRODUCT_OWNER, request_id, story_id, "reject",
                {"reason": said, "revision": str(story["revision"]), "basis": str(story["basis"])},
            ),
        )  # fmt: skip

    return Decision(
        "reject", shown, f"{project_id}:reject:{story_id}:{said}",
        f"story:{story_id}:{story['revision']}:{story['basis']}", carry_out,
    )  # fmt: skip


async def receipt_on_record(pool: Any, request_id: str) -> Receipt | None:
    """The receipt the Product Owner's request was given, if it has one: asked before a
    passphrase is asked for, since a request sent again decides nothing."""
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT disposition, outcome FROM human_actions "
                "WHERE principal = %s AND request_id = %s",
                (PRODUCT_OWNER.identity, request_id),
            )
        ).fetchone()
    return Receipt(row["disposition"], row["outcome"]) if row else None
