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
from relay.application.stories import closure_in, standings_in
from relay.cli.terminal import visible
from relay.domain.story import (
    HumanAction,
    Principal,
    PrincipalKind,
    Receipt,
    StoryState,
    permits_behaviour_work,
)
from relay.runtime import Relay

PRODUCT_OWNER = Principal(PrincipalKind.HUMAN, "product-owner")

# The verbs that carry words of the person's own, and which.
TAKES_TEXT = ("answer", "replace-decision", "start")
TAKES_A_REASON = ("request-changes", "reject", "retry", "pause", "cancel")

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
    # The whole identity of what it is about, however the person named it: what a
    # prepared action keeps, so that it means the same thing when it is confirmed.
    target_id: str = ""
    # In Relay's own words and from identifiers only: what the passphrase is asked for.
    to: str = ""
    # The same basis as earlier builds of Relay worked it out: an action one of them
    # prepared is not stale merely because this build describes the record another way.
    also: tuple[str, ...] = ()


def _evidence(item: Item) -> str:
    # JSON shows every control character below a space as an escape, and everything
    # beyond ASCII too: what a model or a repository wrote cannot drive the terminal.
    return json.dumps(item.evidence, indent=2, sort_keys=True, default=str).replace(
        "\x7f", "\\u007f"
    )


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
    # What each verb has a place for, asked before anything else: a word that is taken
    # is shown, decided on and recorded, and a word with no place is refused here,
    # before a code is given for it or a passphrase asked.
    if (text or "").strip() and verb not in TAKES_TEXT:
        raise CannotDecide(f"to {verb} carries no text: say it again without {text!r}")
    if (reason or "").strip() and verb not in TAKES_A_REASON:
        raise CannotDecide(f"to {verb} carries no reason: say it again without --reason")
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
            verb, f"Start a feature in {project_id} from this problem:\n\n  {visible(problem)}\n",
            f"{project_id}:start:{problem}", f"start:{problem}", start,
            "", f"start a feature in {project_id}",
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
    # Every word that is taken reaches the decision and its receipt, and no word is
    # taken that the decision has no place for: nothing is shown to the person as part
    # of what they confirm and then dropped, or refused after the passphrase was asked.
    if (reason or "").strip() and not needs_reason:
        raise CannotDecide(
            f"to {verb} {item.about} carries no reason: say it again without --reason"
        )
    if (text or "").strip() and verb != "answer":
        raise CannotDecide(f"to {verb} this carries no text: say it again without {text!r}")
    # Trimmed once, here: what is shown, what is decided on, what the receipt records
    # and what a request sent again is compared with are the same words.
    said = ((text if verb == "answer" else reason) or "").strip()
    shown = (
        f"In {project_id}, you are about to {verb}: {item.about}\n\n{_evidence(item)}\n"
        + (f"\nYou say: {visible(said.strip())}\n" if said.strip() else "")
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
            # Held, where the retry is authorised, to all the person was shown of the
            # work: a bill that arrives late for an earlier call changes what they saw.
            return await relay.invocations.decide(by(verb.replace("-", "_"), as_seen=item.basis))
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
        verb, shown, f"{project_id}:{verb}:{item.item_id}:{said}", item.basis, carry_out,
        item.item_id, f"{verb} {item.kind.replace('_', ' ')} {item.handle} in {project_id}",
    )  # fmt: skip


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
        standing_now, as_earlier_builds_said = (
            await standings_in(connection, project_id, feature_id, feature)
            if feature is not None
            else ("", ())
        )
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
        f"  {visible(feature['problem'])}\n"
        + (f"\nYou say: {visible((reason or '').strip())}\n" if verb != "resume" else "")
        + (
            f"\n(the pause this lifts is entry {closure} of the ledger)\n"
            if verb == "resume"
            else ""
        )
    )

    # Its state, whether it is closed, and the last time it was paused, resumed or
    # cancelled: paused and resumed since, it is open as it was and not as it was shown.
    basis = standing_now
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
                {"closure": str(closure), "as_seen": held_to},
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
        verb, shown, f"{project_id}:{verb}:{feature_id}:{reason or ''}:{closure}", basis, carry_out,
        feature_id, f"{verb} feature {feature_id} in {project_id}", as_earlier_builds_said,
    )  # fmt: skip


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
        f"{decision['feature_id']}:\n\n  {visible(decision['text'])}\n\nwith:\n\n  "
        f"{visible(said)}\n"
    )

    async def carry_out(request_id: str) -> Receipt:
        return await relay.decisions.replace(
            HumanAction(
                PRODUCT_OWNER, request_id, str(decision["decision_id"]), "replace", {"text": said}
            )
        )

    return Decision(
        "replace-decision", shown, f"{project_id}:replace:{decision['decision_id']}:{said}",
        f"decision:{decision['decision_id']}", carry_out, str(decision["decision_id"]),
        f"replace decision {decision['number']} of {decision['feature_id']} in {project_id}",
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
    if not permits_behaviour_work(StoryState(story["state"])):
        # The service's own rule, applied here as well: a story that is not being worked
        # on is not rejected this way, and is not prepared to be.
        raise CannotDecide(
            f"story {story_id} is {story['state'].replace('_', ' ')}, not being worked on: "
            "there is nothing of it to reject this way (see `relay inbox` for what is "
            "waiting at a gate)"
        )
    said = (reason or "").strip()
    shown = (
        f"In {project_id}, you are about to reject story {story_id} of {story['feature_id']} "
        f"({story['state']}), which is being worked on:\n\n  {visible(story['title'])}\n\n"
        f"You say: {visible(said)}\n\n(as it stands at revision {story['revision']}, under "
        f"decision basis {story['basis']})\n"
    )

    async def carry_out(request_id: str) -> Receipt:
        return await relay.stories.reject(
            project_id,
            HumanAction(
                PRODUCT_OWNER, request_id, story_id, "reject",
                {"reason": said, "revision": str(story["revision"]),
                 "basis": str(story["basis"]), "state": str(story["state"])},
            ),
        )  # fmt: skip

    return Decision(
        "reject", shown, f"{project_id}:reject:{story_id}:{said}",
        f"story:{story_id}:{story['revision']}:{story['basis']}:{story['state']}", carry_out,
        story_id, f"reject story {story_id} in {project_id}",
    )  # fmt: skip


# The service's word for each verb, as its receipt records it.
_ACTION = {
    "approve": "approve",
    "request-changes": "request_changes",
    "reject": "reject",
    "retry": "retry",
    "set-aside": "set_aside",
    "answer": "answer",
    "replace-decision": "replace",
    "pause": "pause",
    "resume": "lift",
    "cancel": "cancel",
    "start": "start",
}


async def receipt_on_record(
    pool: Any,
    request_id: str,
    project_id: str,
    verb: str,
    target: str,
    text: str | None,
    reason: str | None,
) -> Receipt | None:
    """The receipt the Product Owner's request was given, if it has one and it is this
    request's: asked before a passphrase is asked for, since a request sent again decides
    nothing. The receipt on record says what it was for (the action, what it was done
    to, what was said), and a command is given it only if that is what the command
    asks: so `cancel` is not answered with the receipt of an `approve`, and a receipt
    made by any build of Relay is still its request's own."""
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT action, target, payload, disposition, outcome FROM human_actions "
                "WHERE principal = %s AND request_id = %s",
                (PRODUCT_OWNER.identity, request_id),
            )
        ).fetchone()
    if row is None:
        return None
    recorded, said = str(row["target"]), dict(row["payload"])
    typed = (target or "").strip()
    async with pool.connection() as connection:
        if verb == "start":
            # Recorded against the project it was started in.
            same_target = recorded == project_id
        elif verb in ("pause", "resume", "cancel") or (verb == "reject" and "/" in recorded):
            # A feature or a story, recorded under its project and named in full: these
            # names are not abbreviated, and the same name in another project is
            # another feature.
            same_target = recorded == f"{project_id}/{typed}"
        else:
            # An item or a decision, recorded by its identity. It is this request's only
            # if it is of this project, and if what was typed names it: its identity, the
            # start of it, or for a decision its feature and number.
            of = await (
                await connection.execute(
                    "SELECT project_id FROM gates WHERE gate_id::text = %s UNION ALL "
                    "SELECT project_id FROM questions WHERE question_id::text = %s UNION ALL "
                    "SELECT project_id FROM invocations WHERE invocation_id::text = %s UNION ALL "
                    "SELECT project_id FROM integration_intents WHERE intent_id::text = %s "
                    "UNION ALL SELECT project_id FROM decisions WHERE decision_id::text = %s",
                    (recorded,) * 5,
                )
            ).fetchone()
            named = recorded == typed or (len(typed) >= 6 and recorded.startswith(typed))
            if not named and verb == "replace-decision" and "/" in typed:
                feature_id, _, number = typed.partition("/D")
                numbered = await (
                    await connection.execute(
                        "SELECT 1 FROM decisions WHERE decision_id::text = %s AND project_id = %s "
                        "AND feature_id = %s AND number::text = %s",
                        (recorded, project_id, feature_id, number),
                    )
                ).fetchone()
                named = numbered is not None
            same_target = named and of is not None and of["project_id"] == project_id
    # The same words, trimmed on both sides: an earlier build recorded them as typed.
    words = ("problem",) if verb == "start" else ("text", "reason")
    given = {"problem": text, "text": text, "reason": reason}
    same_words = all(
        str(said.get(each, "")).strip() == (given[each] or "").strip() for each in words
    )
    if row["action"] != _ACTION.get(verb) or not same_target or not same_words:
        raise CannotDecide(
            f"request {request_id} is a request for something else: a request's identity is "
            "used again only to ask for the same thing. Nothing is done"
        )
    return Receipt(row["disposition"], row["outcome"])
