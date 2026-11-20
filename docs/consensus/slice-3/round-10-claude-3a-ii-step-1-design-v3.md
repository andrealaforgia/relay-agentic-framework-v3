# Slice 3a-ii, step 1, design v3: pause, resume and cancel (consolidated)

All five findings of round 9 accepted. This document stands alone: it replaces v2 and v2.1 entirely, so that nothing is lost between amendments (S3-6.2 was exactly that).

## 1. The gate lock

Closure is recorded on the feature: `closed_by` is null, `paused` or `cancelled`. It exists before the first story and covers every roadmap revision.

One row per project, the **gate lock**, is taken first by every workflow transaction, before any behaviour, story, project or feature row: **shared** by everything that authorises or records work; **exclusive** by pause, resume and cancel. No transaction holds it across an oracle run, a Git export or a push. Underneath it the order stays behaviour, story, project, feature.

## 2. What a closure does to each transition

| | Paused | Cancelled |
|---|---|---|
| The project's story slot | Kept; other features wait | Freed in the cancelling transaction: every story of the feature not yet approved or rejected becomes `cancelled`, and the next story of the project is released before commit |
| Releasing a story of this feature | Not done | Never |
| An open gate of this feature | Stays open | Superseded, with its inbox item |
| A first human action on such a gate | Refused, nothing recorded | Stale |
| An exact replay of a recorded action | Its original receipt | Its original receipt |
| An integration intent already authorised | Reconciled to its end | Reconciled to its end; nothing opens or releases as a result |

The slot holder is counted across all of a project's features before candidates for release are filtered. Pause, resume and cancel are human actions with receipts, targeting the feature.

## 3. Starting work and recording work

**Starts work: refused when the feature is closed, by every route**, whether through a claimed command or by calling the operation directly: propose, begin test design, register a contract, run red, run green, integrate (and starting an integration successor), seal, assess, publish a roadmap, publish a candidate (including from the reconciliation pass), announce review tasks, issue a review checkout receipt, `retry_integration`, `reopen_red`, dispute, and `claim`.

**Records work already started: never refused, the evidence always kept.**

| Result arriving | Under pause | Under cancel |
|---|---|---|
| A red or green run | Judged as usual; whatever it makes owed is recorded (section 4) and announced only after resume | Recorded as `superseded`: the execution is kept, the behaviour does not move, nothing becomes owed |
| A review submission for a task with a checkout receipt | Applied as usual; a remediation it causes is owed but not announced | Stored as stale |
| A check batch settling | Runs recorded; **no intent**; the batch is `held` | Runs recorded; no intent; the batch is `abandoned` |
| Reconciliation of an intent | As always | As always |

The intent-insert transaction checks closure itself, under the gate lock. That is the one boundary for trunk: closure first, no intent; intent first, it runs to its end.

## 4. Assignments

An **assignment** is one unit of work Relay wants done, a row of its own: kind (`build`, `remediate`, `review`, `publish_candidate`, `integrate`), behaviour, what it was issued on, and state `wanted`, `granted`, `done` or `retired`.

1. It is created in the transaction that makes the work owed.
2. **Creating one retires what it replaces**, in that transaction: a `remediate` retires the behaviour's earlier `build` or `remediate`; a reopened red retires everything of the behaviour; cancel retires everything of the feature.
3. A command is an announcement of a `wanted` assignment and may be delivered any number of times.
4. **A grant has an identity.** `claim` grants a `wanted` assignment of an open feature and returns the grant's id. The same worker claiming again gets the same grant.
5. **The first effect of an assignment names its grant**, and is accepted only if that grant is still the assignment's current one: the checkout receipt for a review, the successor batch for an integration, the green run for a build or remediation (rule 7).
6. **Pause revokes every grant that has no first effect yet**, under the exclusive gate lock, returning those assignments to `wanted`. A first effect that committed before the pause is preserved and its work may finish. A delayed first effect naming a revoked grant is refused, before and after resume. (S3-6.1)
7. **A green run answers one assignment.** `run_green` captures the behaviour's current build or remediation assignment when it launches and checks it again under the behaviour lock when it settles. If that assignment has since been retired, the run is recorded as superseded: it opens no review cycle and completes nothing. Only an accepted green for the current assignment marks it `done`. (S3-6.3)
8. An assignment is `done` when its effect is on record.

On start, a grant with no first effect is revoked in the same way. In this slice every consumer is in the control-plane process and so is known to be gone; for 3b's separate workers the same guarantee comes from rule 5, since a result naming a superseded grant is refused.

## 5. Integration successors

A held or interrupted batch gets one `integrate` assignment naming the batch and its review cycle. Its grant and the successor batch are created **in one transaction**, under the behaviour lock, and only if **both** hold: the named cycle is still the behaviour's live, satisfied cycle, **and** the named batch is the latest batch of that cycle. Otherwise the assignment is retired with a stale outcome and nothing starts. A batch belongs to a logical request and has a generation within it; a repeated request is answered from the latest generation.

## 6. The reconciliation pass

One idempotent pass per project, run on start and as part of every resume, with no further action needed from anyone. For open features only:

1. revoke grants that have no first effect (start; pause has already done so for a resume);
2. close batches found `running` (start) or `held` as `interrupted`, creating their `integrate` assignments;
3. publish pending candidates, creating their review assignments;
4. evaluate every story gate that could be ready;
5. release the next story if the slot is free;
6. **re-arm the announcement of every `wanted` assignment**, under its same message id, whether or not it was published before. (S3-6.4)

Re-arming and publishing do not undo each other: a command row carries a count of announcements requested and a count published, and the publisher's mark-sent records the count it read. A publish that overlaps the pass leaves the new request standing.

## 6a. Restart with work in flight (added in round 11, in Astra's words)

These three rules were accepted from the round 10 review and are part of this design.

- **A review checked out by a consumer that is gone (S3-7.1).** At startup, for each still-required review with no effective submission, Relay revokes the dead consumer's grant even if a checkout receipt exists, retains that receipt as history, and returns the assignment to `wanted` for a fresh grant and checkout when the feature is open.
- **A batch left running across a restart, whatever the feature's state (S3-7.2).** Before dispatch at startup, Relay marks each dead consumer's still-current `running` batch `interrupted` for an open feature, `held` for a paused feature, or `abandoned` for a cancelled feature, preserving its runs and allowing a held batch to produce one successor on resume.
- **When a green run has started (S3-7.3).** Before launching the oracle, `run_green` commits a run-start record naming the attempt, assignment and grant under the gate lock; this is the first effect for pause, and startup marks any such unfinished run interrupted and makes still-required work eligible for a fresh grant when open.

Consequences for the rules above: in section 4, rule 5's first effect for a build or remediation is the run-start record, not the finished green; in section 6, step 1 and step 2 at startup cover paused and cancelled features too, as these sentences say, while steps 3 to 6 remain for open features only.

Tests added: a review checked out, the process killed, restart: the receipt is kept, the review is granted afresh and done once. A pause with a check running, the process killed before the batch settles, restart while paused, then resume: one successor, one integration. A pause after the run-start record is committed lets that green finish and be judged; a pause before it revokes the grant.

## 7. What "nothing is delivered" means

No grant after a closure commits. A publisher that selects after the closure skips the commands of a paused feature. A row selected before may still reach Redis, and an entry already there may still reach a worker; neither can be granted.

## Tests

Locking: pause against the first roadmap publication and against the release of a planned story, both commit orders, the barrier waiting on PostgreSQL's report of the lock wait.

Closure: cancel at an open gate with another feature waiting (gate superseded, the waiter released in that transaction, a later approval stale); a fresh approval under pause refused, its replay after resume effective once.

Assignments: the original build command delivered after a remediation is owed cannot be claimed; a review granted, then pause, then its checkout refused, then resume with no restart, announced again and done once; the other order (receipt first, then pause) lets the review finish; two green runs for one build, the second settling after a remediation is owed, recorded as superseded with no cycle opened; a first effect naming a revoked grant refused after resume.

Successors: hold A, resume, delay A's command, start B on the same cycle: A stale, B current, B failing sends the candidate back once; a superseded cycle's latest batch starts nothing; kill the consumer after the successor batch exists and find one settled intent after restart.

Reconciliation: a green accepted during a pause is published and announced after resume with nothing else called; a story whose last integration settled during a pause gets its gate after resume; an assignment already published and unclaimed, Redis emptied, is delivered again under the same message id, including when a publish overlaps the pass.

Dispatch: the publisher held between selecting and appending across a pause; the delivered command cannot be claimed until resume.

**Exit tests**, four reachable histories through the production entry points, each once with resume and once with cancel:

1. a red run in flight; 2. a review in flight; 3. the last regression check in flight. For these: what was observed is on record; no grant, no new intent and no trunk movement by that work while closed; after a restart with Redis empty and a resume, one effective grant or successor per assignment and one integration; after a cancel, the slot freed for a waiting feature and late results kept as audit only.
4. an intent already authorised. Only the previously authorised commit can reach trunk; its settlement is recorded once; and while the feature is closed it opens no gate and releases no successor.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 design v3).
