# Consensus record: slice 2 (reviews and integration)

## Design, before any code

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 7 blocking | Among them: a blocker raised during the regression run could be bypassed; a commit does not identify a review cycle; a finding could be closed against a sibling commit |
| 2 | CHALLENGE, 6 blocking | Reviewers could see different trees; a fix on one candidate cleared the next; the capability could leak before publication; a late failing check could contradict an authorised integration |
| 3 | CHALLENGE, 2 blocking | A fetched commit needs a retention ref; a rejected push does not settle an intent while an earlier send may still land |

Design v3.1 records the three accepted amendments from round 3.

## Slice 2a as built

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 6 blocking | Adoption could bind a rejected green; finding ids collided; recoverable commits declared lost; a transient Git failure became permanent; lock order; re-announcement defeated deduplication |
| 2 | CHALLENGE, 3 blocking | An interrupted fetch read as "gone"; a listing failure stopping other projects; stderr words mistaken for a remote refusal |
| 3 | CHALLENGE, 3 blocking | An unpublished candidate ref searched for ever; a failed ancestry check read as "no"; an uncertain push status read as "ref exists" |
| 4 | CHALLENGE, 2 blocking | An empty mirror directory; one unreadable mirror stopping adoption for others |
| 5 | ACCEPT_WITH_RESERVATIONS | Concurrent deferral events could mislead (since closed) |

All reviewer replies are from `gpt-6.2-astra`. Astra ran the unit tests itself each round and could not run the integration tests in its read-only workspace; those results and the mutation checks are Claude's reports. Agreement between two models is not evidence of correctness.

The design rounds kept the protocol stable: no round of the code review overturned it. What the code review found was failure handling around Git, where one mistake recurred in several forms: reading "could not find out" as "no". That is now fixed at its root in `git_answers_yes`.

Slice 2b (integration) is designed (v3.1) and not built.
