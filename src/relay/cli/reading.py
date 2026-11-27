"""What a person, or a model on their behalf, may read: where things stand, what is
waiting, the roadmap and the ledger. Each is a set of facts read from the record, and its
text and its JSON are the same facts."""

from __future__ import annotations

import dataclasses
from typing import Any

from relay.application.inbox import Inbox, Item
from relay.persistence.database import Pool


async def status(pool: Pool, project_id: str) -> dict[str, Any]:
    """Each feature and where it stands, with its stories and their behaviours; what is
    owed and to whom; and how much is waiting for the Product Owner."""
    async with pool.connection() as connection:
        features = await (
            await connection.execute(
                "SELECT feature_id, problem, state, closed_by FROM features "
                "WHERE project_id = %s ORDER BY feature_id",
                (project_id,),
            )
        ).fetchall()
        stories = await (
            await connection.execute(
                "SELECT story_id, feature_id, title, state FROM stories "
                "WHERE project_id = %s AND state <> 'withdrawn' ORDER BY position, story_id",
                (project_id,),
            )
        ).fetchall()
        behaviours = await (
            await connection.execute(
                "SELECT behaviour_id, story_id, summary, state FROM behaviours "
                "WHERE project_id = %s ORDER BY behaviour_id",
                (project_id,),
            )
        ).fetchall()
        owed = await (
            await connection.execute(
                "SELECT kind, role, state, feature_id, story_id, behaviour_id, parked, "
                "grant_id IS NOT NULL AS taken FROM assignments WHERE project_id = %s "
                "AND state IN ('wanted', 'granted', 'asked') ORDER BY created_at",
                (project_id,),
            )
        ).fetchall()
    waiting = await Inbox(pool).items(project_id)
    return {
        "project": project_id,
        "features": [
            {
                "feature": feature["feature_id"],
                "problem": feature["problem"],
                "state": feature["closed_by"] or feature["state"],
                "stories": [
                    {
                        "story": story["story_id"],
                        "title": story["title"],
                        "state": story["state"],
                        "behaviours": [
                            {
                                "behaviour": each["behaviour_id"],
                                "summary": each["summary"],
                                "state": each["state"],
                            }
                            for each in behaviours
                            if each["story_id"] == story["story_id"]
                        ],
                    }
                    for story in stories
                    if story["feature_id"] == feature["feature_id"]
                ],
            }
            for feature in features
        ],
        "owed": [
            {
                "work": each["kind"],
                "of": each["behaviour_id"] or each["story_id"] or each["feature_id"],
                "to": each["role"],
                "state": (
                    "stopped, waiting for a person"
                    if each["parked"]
                    else "waiting on a question"
                    if each["state"] == "asked"
                    else "taken up"
                    if each["taken"]
                    else "waiting for a worker"
                ),
            }
            for each in owed
        ],
        "waiting_for_the_product_owner": len(waiting),
    }


def status_text(facts: dict[str, Any]) -> str:
    lines = [f"project {facts['project']}"]
    if not facts["features"]:
        lines.append('  no feature yet: start one with `relay feature start "<the problem>"`')
    for feature in facts["features"]:
        lines.append(f"  {feature['feature']}  {feature['state']}  {feature['problem']}")
        for story in feature["stories"]:
            lines.append(f"    {story['story']}  {story['state']}  {story['title']}")
            for each in story["behaviours"]:
                lines.append(f"      {each['behaviour']}  {each['state']}  {each['summary']}")
    lines.append("owed:" if facts["owed"] else "owed: nothing")
    for each in facts["owed"]:
        lines.append(f"  {each['work']} of {each['of']}, to the {each['to']}: {each['state']}")
    waiting = facts["waiting_for_the_product_owner"]
    lines.append(
        f"waiting for the Product Owner: {waiting} (see `relay inbox`)"
        if waiting
        else "waiting for the Product Owner: nothing"
    )
    return "\n".join(lines)


def item_facts(item: Item) -> dict[str, Any]:
    return dataclasses.asdict(item)


def inbox_text(items: list[Item]) -> str:
    if not items:
        return "nothing is waiting for the Product Owner"
    lines = []
    for item in items:
        lines.append(f"{item.item_id[:8]}  {item.kind:<12}  {item.about}")
        lines.append(
            f"          can be: {', '.join(item.actions)}   (`relay show {item.item_id[:8]}`)"
        )
    return "\n".join(lines)


def item_text(item: Item) -> str:
    import json

    return (
        f"{item.about}\n\nitem {item.item_id} ({item.kind}), waiting since {item.waiting_since}\n"
        f"can be: {', '.join(item.actions)}\n\n"
        f"{json.dumps(item.evidence, indent=2, sort_keys=True, default=str)}\n\n"
        f"as shown: {item.basis}"
    )


async def roadmap(pool: Pool, project_id: str, feature_id: str | None) -> dict[str, Any]:
    """A feature's roadmap as it is in force, and whether a proposal is waiting."""
    async with pool.connection() as connection:
        features = await (
            await connection.execute(
                "SELECT feature_id, problem, state FROM features WHERE project_id = %s "
                "AND (%s::text IS NULL OR feature_id = %s) ORDER BY feature_id",
                (project_id, feature_id, feature_id),
            )
        ).fetchall()
        if feature_id is None and len(features) > 1:
            # The one being delivered, if there is one; else the latest.
            live = [each for each in features if each["state"] not in ("completed", "cancelled")]
            features = (live or features)[-1:]
        if not features:
            raise LookupError(
                f"there is no feature {feature_id!r} in {project_id}"
                if feature_id
                else f"there is no feature in {project_id} yet"
            )
        feature = features[0]
        stories = await (
            await connection.execute(
                "SELECT story_id, title, outcome, criteria, state, roadmap_revision FROM stories "
                "WHERE project_id = %s AND feature_id = %s AND state <> 'withdrawn' "
                "ORDER BY position, story_id",
                (project_id, feature["feature_id"]),
            )
        ).fetchall()
        proposal = await (
            await connection.execute(
                "SELECT gate_id FROM gates WHERE project_id = %s AND feature_id = %s "
                "AND kind = 'roadmap' AND state = 'open'",
                (project_id, feature["feature_id"]),
            )
        ).fetchone()
    return {
        "feature": feature["feature_id"],
        "problem": feature["problem"],
        "state": feature["state"],
        "stories": [
            {
                "story": story["story_id"],
                "title": story["title"],
                "outcome": story["outcome"],
                "criteria": dict(story["criteria"]),
                "state": story["state"],
            }
            for story in stories
        ],
        "proposal_waiting": str(proposal["gate_id"]) if proposal else None,
    }


def roadmap_text(facts: dict[str, Any]) -> str:
    lines = [f"{facts['feature']}  {facts['state']}  {facts['problem']}"]
    if not facts["stories"]:
        lines.append("  no story yet")
    for story in facts["stories"]:
        lines.append(f"  {story['story']}  {story['state']}  {story['title']}")
        lines.append(f"      so that: {story['outcome']}")
        for name, text in story["criteria"].items():
            lines.append(f"      {name}: {text}")
    if facts["proposal_waiting"]:
        lines.append(
            f"a proposal is waiting for the Product Owner: `relay show "
            f"{facts['proposal_waiting'][:8]}`"
        )
    return "\n".join(lines)


async def ledger(
    pool: Pool, project_id: str, subject: str | None, since: int, limit: int = 200
) -> list[dict[str, Any]]:
    """The events of a project, in order: Relay's record of what happened."""
    async with pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT sequence, event_type, subject_id, payload, recorded_at FROM events "
                "WHERE project_id = %s AND sequence > %s AND (%s::text IS NULL OR subject_id = %s) "
                "ORDER BY sequence LIMIT %s",
                (project_id, since, subject, subject, limit),
            )
        ).fetchall()
    return [
        {
            "sequence": row["sequence"],
            "event": row["event_type"],
            "subject": row["subject_id"],
            "at": row["recorded_at"].isoformat(timespec="seconds"),
            "payload": row["payload"],
        }
        for row in rows
    ]


def ledger_text(events: list[dict[str, Any]]) -> str:
    if not events:
        return "no events"
    return "\n".join(
        f"{each['sequence']:>6}  {each['at']}  {each['event']:<40}  {each['subject'] or ''}"
        for each in events
    )
