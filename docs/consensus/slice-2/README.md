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

## Slice 2b as built

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 7 blocking | A partial batch could authorise trunk; a stale reading could release the project's slot; "trunk contains the candidate" taken as integrated; relative repository paths; a duplicate check; a phantom send; a cycle reused after a conflict |
| 2 | CHALLENGE, 4 blocking | The intent built from data the check runner held; a spawn failure counted as a push in flight; a mirror lost after authorisation; an unbounded read of trunk |
| 3 | CHALLENGE, 4 blocking | "Never started" not proven; the last answer after a refusal; the runner naming the batch; an unbounded recovery fetch |
| 4 | CHALLENGE, 3 blocking | All in the Git launcher rewritten in round 3: an error after Git ran read as "never started"; a reader thread that could hang; readers and descriptors kept after a timeout |
| 5 | CHALLENGE, 3 blocking | The launcher again: a filename on an error proves nothing; a capture read error escaping; a lost exit status read as success, which could record a refused push as integrated |
| 6 | CHALLENGE, 2 blocking | A signal to a process id no longer owned; a capture close error escaping |
| 7 | CHALLENGE, 2 blocking | A reaper between the ownership check and the signal; a close error replacing a cancellation |
| 8 | ACCEPT_WITH_RESERVATIONS | Nothing new |

Claude challenged one finding (round 7, the reaper between check and signal) instead of fixing it, on the grounds that nothing in the control-plane process reaps a child it did not start. Astra looked, found no such component, and withdrew the blocking classification.

### Reservation carried forward

`git_result` signals a Git it has given up on only after asking whether that process is still its own unreaped child. Between the question and the signal the process id cannot be held. This is safe only while **nothing else in the control-plane process reaps a child it did not start**. Any future component that waits on all children (`waitpid(-1)`), or on a process id it was not given, breaks the rule and must be reviewed against it.

### What the review of 2b showed

The design held again: no round overturned the integration protocol. Rounds 1 to 3 found real gaps at the authority boundary. Rounds 4 to 7 were all about one function, the Git launcher, and three of those rounds were caused by Claude's own rewrites of it: a helper thread reading pipes, then an assumption about how a failed launch is reported that Claude asked Astra to attack and Astra broke. The launcher that survived has no threads and no pipes, and treats every failure after the launch is attempted as "outcome unknown".

Almost every integration test passed on its first run, so each guard was instead broken on purpose to see a test fail. Across the rounds that found guards with no test and several that were redundant and were removed.
