# Slice 3a-i as built: two stories, a human gate, a restart

Built to design v2 with the v2.1 amendments. Please review the code, not this description.

## Where to look

- `src/relay/domain/story.py`: principals, story states, sealing, choosing the next story, exact replays.
- `src/relay/application/stories.py`: `Stories` (`open_feature`, `publish_roadmap`, `seal`, `assess`, `decide`, `inbox`), and the functions other services call inside their own transactions: `add_behaviour_to_story`, `require_story_at_work`, `evaluate_story_gate`, `release_next`.
- `src/relay/application/behaviours.py`: `propose` now takes a story and criteria; `_changing(starts=...)` refuses to start work on a behaviour whose story is not being worked on.
- `src/relay/application/integration.py`: the same refusal at the start of `integrate`; `_converged` evaluates the story gate in the transaction that records the integration.
- `src/relay/persistence/migrations/0007_stories.sql`.
- Tests: `tests/integration/test_story_gate.py` (19), `tests/unit/test_story_rules.py`.

## What it does

1. A human opens a feature. The Analyst publishes a roadmap, which opens a roadmap gate. Nothing is released until a person approves it.
2. `release_next(project)` runs in the transaction of every gate decision, under the project lock, and releases at most one story: the first planned story of an approved roadmap whose predecessors are approved, looking at the project's features in the order they were opened, and only if no story of the project is released, at its gate, or under a change request.
3. A behaviour belongs to a story (`NOT NULL`, foreign key) and names the criteria it demonstrates. Proposing one makes a new list revision. Work may start on a behaviour only while its story is `released` or `changes_requested`; with the project default, a behaviour enters test design only when every earlier one of its story is integrated.
4. The Verifier seals the list (non-empty, every criterion covered, no unknown criterion). The Analyst assesses the sealed list, once.
5. `evaluate_story_gate` runs on seal, on assessment and on each integration. When the latest list is sealed, assessed and wholly integrated, the story becomes `awaiting_approval` and a gate is opened with its manifest stored whole, an inbox row, and an outbox notice, in one transaction.
6. `decide(HumanAction)` requires a human principal, locks story, feature, project, then the gate. An exact replay of a recorded request returns its receipt; the same principal and request id with anything else is refused. A decided gate, or one whose basis differs from what a gate would be opened on now, makes the action stale (and supersedes an open gate). Otherwise the outcome, the inbox change, the event, any release and the receipt commit together.

## Departures from the design, and what is not done

1. **Principals are on the new operations only.** `Stories` takes one everywhere. `Behaviours`, `Reviews` and `Integration` do not yet: their callers are still tests. I intend to add them in 3b, when a worker first calls them and there is a boundary to establish the principal at. Say if you want it sooner.
2. **`behaviours_in_order` is a project setting**, default true. The specification says "default to one active implementation story", and the slice 2 tests need several behaviours in flight to exercise the integration slot. Those tests open their project with it off.
3. **One roadmap per feature** for now; revisions come with corrections in 3a-ii. A change request at a roadmap gate marks that roadmap superseded and nothing more.
4. **`request_changes` at a story gate** moves the story to `changes_requested` and bumps its revision. What the Verifier then does is 3a-ii.
5. **No task records yet** (your answer 4). No command changed in this step; I would add them with the worker in 3b unless you see a reason to settle the table now.
6. **Decisions in the manifest are an empty list** until questions exist (3a-ii). Usage is the string `unknown`.
7. The `integrate` refusal for a story not being worked on raises `ActionRefused`, where the rest of `integrate` raises `IntegrationRefused`.

## Verification

`make check` passes: 444 tests against real PostgreSQL, Redis, Git and Docker.

The gate tests passed on their first run, so I broke 27 guards one at a time. 24 were caught at once. The three that were not each now have a test: an assessment of an earlier list must not open the gate for a later one; a second, different request on an already decided story gate must be stale; and a roadmap nobody approved must release nothing when another gate is decided.

Two tests write to the database directly, and say so: a story forced to its gate with a behaviour still to start, and a gate's basis changed behind it. No path in this step produces either.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-i as built).
