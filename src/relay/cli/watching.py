"""`relay watch`: how the Product Owner observes the agents at work (PO-8, PO-9).

It reads PostgreSQL and never writes. Three views, as the earlier Relay had them:

    the board    which processes are alive, the feature being delivered with its stories
                 and behaviours, what is owed and to whom and for how long, each call in
                 flight, and what is waiting for the Product Owner
    --events     the ledger as it grows, one line an event
    --tokens     what calls have used, as known now

The board is read afresh every two seconds. Something that comes and goes between two
readings is not seen on it; it is in the ledger, which `--events` follows.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from relay.application.leadership import installation_of, lock_key
from relay.application.usage import account_text, live_usage
from relay.application.workers import is_alive
from relay.cli import reading
from relay.cli.terminal import visible
from relay.persistence.database import Pool
from relay.runtime import ROLES

EVERY = 2.0
_CLEAR = "\x1b[2J\x1b[H"


def _for(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


async def board(pool: Pool, project_id: str) -> dict[str, Any]:
    """What the board shows, as facts."""
    async with pool.connection() as connection:
        installation = await installation_of(connection)
        led = await (
            await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
                "AND l.objsubid = 2 AND l.classid = %s::bigint::oid AND l.objid = 1) AS led",
                (lock_key(installation),),
            )
        ).fetchone()
        workers = await (
            await connection.execute("SELECT worker_id, role FROM workers ORDER BY role")
        ).fetchall()
        alive = [
            str(each["role"]) for each in workers if await is_alive(connection, each["worker_id"])
        ]
        in_flight = await (
            await connection.execute(
                "SELECT v.role, v.model, a.kind, a.feature_id, a.story_id, a.behaviour_id, "
                "extract(epoch FROM clock_timestamp() - v.started_at) AS running "
                "FROM invocations v JOIN assignments a USING (assignment_id) "
                "WHERE v.project_id = %s AND v.state = 'intended' ORDER BY v.started_at",
                (project_id,),
            )
        ).fetchall()
        waits = await (
            await connection.execute(
                "SELECT kind, feature_id, story_id, behaviour_id, "
                "extract(epoch FROM clock_timestamp() - created_at) AS waiting "
                "FROM assignments WHERE project_id = %s AND state IN ('wanted', 'granted') "
                "ORDER BY created_at",
                (project_id,),
            )
        ).fetchall()
    standing = await reading.status(pool, project_id)
    since = {
        (each["kind"], each["behaviour_id"] or each["story_id"] or each["feature_id"]): float(
            each["waiting"]
        )
        for each in waits
    }
    return {
        "project": project_id,
        "processes": {
            "control_plane": "running" if led and led["led"] else "not running",
            "workers": {role: alive.count(role) for role in ROLES},
        },
        "features": standing["features"],
        "owed": [
            {**each, "for_seconds": int(since.get((each["work"], each["of"]), 0))}
            for each in standing["owed"]
        ],
        "calls_in_flight": [
            {
                "role": each["role"],
                "model": each["model"],
                "work": each["kind"],
                "of": each["behaviour_id"] or each["story_id"] or each["feature_id"],
                "for_seconds": int(each["running"]),
            }
            for each in in_flight
        ],
        "waiting_for_the_product_owner": standing["waiting_for_the_product_owner"],
    }


def board_text(facts: dict[str, Any]) -> str:
    processes = facts["processes"]
    workers = "  ".join(
        f"{role} {count if count else '-'}" for role, count in processes["workers"].items()
    )
    lines = [
        f"relay watch: {facts['project']}",
        f"control plane: {processes['control_plane']}"
        + ("" if processes["control_plane"] == "running" else "   (start it: `relay up`)"),
        f"workers:       {workers}",
        "",
    ]
    if not facts["features"]:
        lines.append('no feature yet: start one with `relay feature start "<the problem>"`')
    for feature in facts["features"]:
        lines.append(f"{feature['feature']}  {feature['state']}  {visible(feature['problem'])}")
        for story in feature["stories"]:
            lines.append(f"  {story['story']}  {story['state']:<18} {visible(story['title'])}")
            for each in story["behaviours"]:
                lines.append(
                    f"    {each['behaviour']}  {each['state']:<16} {visible(each['summary'])}"
                )
    lines.append("")
    lines.append("in flight:" if facts["calls_in_flight"] else "in flight: no call")
    for each in facts["calls_in_flight"]:
        lines.append(
            f"  {each['role']:<9} {each['work']} of {each['of']}  on {visible(each['model'])}  "
            f"for {_for(each['for_seconds'])}"
        )
    lines.append("owed:" if facts["owed"] else "owed: nothing")
    for each in facts["owed"]:
        lines.append(
            f"  {each['work']} of {each['of']}, to the {each['to']}: {each['state']}, "
            f"for {_for(each['for_seconds'])}"
        )
    waiting = facts["waiting_for_the_product_owner"]
    lines.append("")
    lines.append(
        f"waiting for the Product Owner: {waiting}   (see `relay inbox`)"
        if waiting
        else "waiting for the Product Owner: nothing"
    )
    return "\n".join(lines)


async def watch(
    pool: Pool,
    project_id: str,
    view: str,
    *,
    once: bool = False,
    every: float = EVERY,
    as_json: bool = False,
    out: Callable[[str], None] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Show a view until told to stop: redrawn, for the board and the tokens; added to,
    for the events. With `once`, shown once and left: for a pipe, or a look."""
    say = out or (lambda text: print(text, flush=True))
    redrawn = sys.stdout.isatty() and not once and out is None
    seen = 0
    while True:
        if view == "events":
            new = await reading.ledger(pool, project_id, None, seen, limit=500)
            if new:
                seen = int(new[-1]["sequence"])
                say(
                    "\n".join(json.dumps(each, default=str) for each in new)
                    if as_json
                    else reading.ledger_text(new)
                )
        else:
            facts = (
                await live_usage(pool, project_id)
                if view == "tokens"
                else await board(pool, project_id)
            )
            text = (
                json.dumps(facts, indent=2, default=str)
                if as_json
                else account_text(facts)
                if view == "tokens"
                else board_text(facts)
            )
            say((_CLEAR if redrawn else "") + text)
        if once or (stop is not None and stop.is_set()):
            return
        if stop is None:
            await asyncio.sleep(every)
        else:
            try:
                await asyncio.wait_for(stop.wait(), every)
                return
            except TimeoutError:
                pass
