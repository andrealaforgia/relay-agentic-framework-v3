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
_PAGE = 500


def _for(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


async def board(pool: Pool, project_id: str) -> dict[str, Any]:
    """What the board shows, as facts: one frame, of one moment."""
    async with pool.connection() as connection, connection.transaction():
        # Everything of the workflow in a frame is read from one snapshot: a reviewer
        # who submits while the board is being read is, in that frame, either still
        # reviewing or done, and never both. Who is alive is a matter of locks held at
        # this instant, and is read beside it.
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        installation = await installation_of(connection)
        standing = await reading.status_in(connection, project_id, for_the_board=True)
        in_flight = await (
            await connection.execute(
                "SELECT v.role, v.model, v.worker_id, a.kind, a.feature_id, a.story_id, "
                "a.behaviour_id, extract(epoch FROM clock_timestamp() - v.started_at) AS running "
                "FROM invocations v JOIN assignments a USING (assignment_id) "
                "WHERE v.project_id = %s AND v.state = 'intended' ORDER BY v.started_at",
                (project_id,),
            )
        ).fetchall()
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
        living = {
            str(each["worker_id"])
            for each in workers
            if await is_alive(connection, each["worker_id"])
        }
    alive = [str(each["role"]) for each in workers if str(each["worker_id"]) in living]
    return {
        "project": project_id,
        "processes": {
            "control_plane": "running" if led and led["led"] else "not running",
            "workers": {role: alive.count(role) for role in ROLES},
        },
        # Every worker there is a record of, with whether its lock is held now.
        "workers": [
            {
                "worker": str(each["worker_id"]),
                "role": str(each["role"]),
                "alive": str(each["worker_id"]) in living,
            }
            for each in workers
        ],
        "features": standing["features"],
        "owed": [
            {
                **each,
                # Whoever holds it, and whether they are still there: a holder that has
                # gone has not been recovered from yet, and the work is not moving.
                "holder_alive": (
                    (each["held_by"] in living)
                    if each["holder"] == "worker"
                    else bool(led and led["led"])
                    if each["holder"] == "control plane"
                    else None
                ),
            }
            for each in standing["owed"]
        ],
        "calls_in_flight": [
            {
                "role": each["role"],
                "model": each["model"],
                "work": each["kind"],
                "of": each["behaviour_id"] or each["story_id"] or each["feature_id"],
                "for_seconds": int(each["running"]),
                "worker": str(each["worker_id"]),
                "worker_alive": str(each["worker_id"]) in living,
            }
            for each in in_flight
        ],
        "waiting_for_the_product_owner": standing["waiting_for_the_product_owner"],
        "waiting": standing["waiting"],
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
    name = _names([each["worker"] for each in facts["workers"]])
    lines.append("in flight:" if facts["calls_in_flight"] else "in flight: no call")
    for each in facts["calls_in_flight"]:
        lines.append(
            f"  {each['role']:<9} {each['work']} of {each['of']}  on {visible(each['model'])}  "
            f"for {_for(each['for_seconds'])}  by worker {name(each['worker'])}"
            # What the board knows, and no more: the worker's lock is not held. The
            # provider may be running still, and its result may yet arrive.
            + (
                ""
                if each["worker_alive"]
                else " (its worker's lock is GONE: whether the call is still running is not known)"
            )
        )
    lines.append("owed:" if facts["owed"] else "owed: nothing")
    for each in facts["owed"]:
        held = ""
        if each["holder"]:
            who = (
                f"worker {name(each['held_by'])}"
                if each["holder"] == "worker"
                else "the control plane"
            )
            held = (
                f"; held by {who} "
                f"({'alive' if each['holder_alive'] else 'GONE: not yet recovered'})"
                + (
                    f" for {_for(each['held_for_seconds'])}"
                    if each["held_for_seconds"] is not None
                    else ""
                )
            )
        lines.append(
            f"  {each['work']} of {each['of']}, to the {each['to']}: {each['state']}, "
            f"owed for {_for(each['owed_for_seconds'])}{held}"
        )
    lines.append("")
    if not facts["waiting"]:
        lines.append("waiting for the Product Owner: nothing")
    else:
        lines.append(f"waiting for the Product Owner: {len(facts['waiting'])}")
        for each in facts["waiting"]:
            lines.append(
                f"  {each['handle']}  {each['kind'].replace('_', ' ')}: {each['about']}   "
                f"(`relay show {each['handle']}`)"
            )
    return "\n".join(lines)


def _names(workers: list[str]) -> Callable[[str], str]:
    """A short name for each worker that no other worker in the frame shares. Identities
    made in the same minute begin alike, so eight characters do not tell two workers
    started together apart."""

    def name(worker: str) -> str:
        for length in range(8, len(worker) + 1):
            if sum(1 for other in set(workers) if other.startswith(worker[:length])) <= 1:
                return worker[:length]
        return worker

    return name


async def watch(
    pool: Pool,
    project_id: str,
    view: str,
    *,
    once: bool = False,
    every: float = EVERY,
    as_json: bool = False,
    feature_id: str | None = None,
    out: Callable[[str], None] | None = None,
    stop: asyncio.Event | None = None,
    terminal: bool | None = None,
) -> None:
    """Show a view until told to stop: redrawn, for the board and the tokens; added to,
    for the events. With `once`, shown once and left: for a pipe, or a look."""
    say = out or (lambda text: print(text, flush=True))
    # A terminal is cleared between frames a person reads. JSON is for a program: it is
    # never preceded by anything that is not JSON, on a terminal or off one.
    is_a_terminal = sys.stdout.isatty() if terminal is None else terminal
    redrawn = is_a_terminal and not once and not as_json
    seen = 0
    # For one look at the events: everything there is at this moment, and no more. What
    # arrives while it is being read is for the next look, and the command ends.
    until = await _last_event(pool, project_id) if view == "events" and once else None
    while True:
        if view == "events":
            while True:
                # A page at a time until there is no more: a ledger longer than a page
                # is shown whole, and is not cut off at the page's end.
                new = await reading.ledger(pool, project_id, None, seen, limit=_PAGE)
                if until is not None:
                    new = [each for each in new if int(each["sequence"]) <= until]
                if not new:
                    break
                seen = int(new[-1]["sequence"])
                say(
                    "\n".join(json.dumps(each, default=str) for each in new)
                    if as_json
                    else reading.ledger_text(new)
                )
                if stop is not None and stop.is_set():
                    # Told to stop while there was more to read: a ledger that grows
                    # faster than it is read would otherwise never let go.
                    return
        else:
            facts = (
                await live_usage(pool, project_id, feature_id)
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


async def _last_event(pool: Pool, project_id: str) -> int:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT coalesce(max(sequence), 0) AS last FROM events WHERE project_id = %s",
                (project_id,),
            )
        ).fetchone()
    return int(row["last"]) if row else 0
