# Slice 3a-ii, step 1, design v2.1

All six findings accepted. Sections 1 and 2 of v2 stand (the gate lock; what pause and cancel do to each transition), with your note that the slot holder is counted across all features before candidates are filtered. Sections 3 to 5 of v2 are replaced by what follows.

Five of the six findings have one cause: v2 inferred "what work is owed" from outbox rows and behaviour state. v2.1 records it.

## 3. Assignments

An **assignment** is one unit of work Relay wants done, as a row of its own:

- its kind (`build`, `remediate`, `review`, `publish_candidate`, `integrate`), its behaviour, and what it was issued on (the accepted red; the closed cycle and its findings or failed checks; the review task; the cycle to publish; the parent batch and its cycle);
- its state: `wanted`, `granted`, `done` or `retired`.

Rules:

1. An assignment is created in the transaction that makes the work owed: an accepted red creates a `build`; a blocking gate or a failed batch creates a `remediate`; a verified green creates a `publish_candidate`; publication creates the `review` assignments; a held or interrupted batch creates an `integrate`.
2. **Creating one retires what it replaces**, in the same transaction. A `remediate` retires the behaviour's earlier `build` or `remediate`. A verified green retires the build or remediation it answers. A reopened red retires everything of that behaviour. Cancel retires everything of the feature. (S3-5.2: the original build command names a retired assignment and can never be granted again, though the behaviour is `implementing` on the same red.)
3. A command is only an announcement of a `wanted` assignment. It can be delivered any number of times. Its outbox row is `held` while the feature is paused.
4. **`claim`** grants only a `wanted` assignment whose feature is open, and commits the grant. Claiming a granted assignment again by the same worker returns the same grant.
5. An assignment is `done` when its effect is on record: the green run it led to; the effective submission; the published cycle; the successor batch.

## 4. From grant to first effect (S3-5.1)

- **`integrate`**: the grant and the successor batch are created in one transaction. There is no moment at which one exists without the other.
- **`review`**: the grant is not the start of a review. The start is the **checkout receipt**, and closure is checked again, under the gate lock, in the transaction that issues it, after the Git export. A review granted and then paused before its receipt has not started; the receipt is refused.
- **Every kind**: on start, a `granted` assignment with no effect on record goes back to `wanted`. With one control plane, a grant found on start belongs to a consumer that is gone; slice 5's leases will make that judgement properly when there can be several. Paid-call recovery is 3b.

Tests: grant the integration request and kill the consumer before the batch (now impossible to separate; the test shows one transaction); grant a review, pause, `prepare_review` refused, resume, the review is announced again and done once; a granted build with no green found on start is wanted again.

## 5. Resume is the start-up pass (S3-5.3)

One idempotent **reconciliation pass** per project does everything that could be owed with no event left to cause it:

1. return orphaned grants to `wanted` (start only);
2. close batches found `running` (start only) or `held` as `interrupted`, creating their `integrate` assignments;
3. publish pending candidates, creating their review assignments;
4. evaluate every story gate that could be ready;
5. release the next story, if the slot is free;
6. announce every `wanted` assignment of an open feature that is not currently announced.

It runs on start, and **in the transaction sequence of a resume**, with no further action from anyone. So a green accepted during a pause is published and announced after the resume, and a story whose last integration settled during a pause gets its gate.

Tests: both of those cases, each asserting the outcome after resume with nothing else called.

## 6. Only the cycle's current batch may be replaced (S3-5.4)

An `integrate` assignment names its parent batch. Starting its successor, under the behaviour lock, requires the parent to be **the latest batch of that cycle**, and replaces it atomically. If an independent request has since started a newer batch on the cycle, the assignment is retired with a stale outcome and nothing starts. The slice 2b rule is kept: only the cycle's current batch decides.

Test: hold A; resume; delay A's command; start B on the same cycle; A's command is then stale; B stays current, and B failing sends the candidate back exactly once.

## 7. What "nothing is delivered" means (S3-5.5)

The guarantee is: **no grant after a closure commits.** A publisher that selects after the closure skips held rows. A row selected before the closure may still reach Redis afterwards, and an entry already in Redis may still reach a worker; neither can be granted. Physical publication is not put under the gate lock.

Test: hold the publisher between selecting a row and appending it, pause, let it append, and show the delivered command cannot be claimed until resume.

## Exit tests, as reachable histories (S3-5.6)

Four separate cases through the production entry points, each with a barrier, each run once with resume and once with cancel:

1. a red run in flight;
2. a review in flight (granted, then checked out);
3. the last regression check in flight;
4. an integration intent already authorised.

Each asserts: what was observed is on record; no grant and no trunk movement while closed; after a restart with Redis empty and a resume, one effective grant or one successor per assignment and one integration; after a cancel, the slot freed for a waiting feature and late results kept as audit only (case 4: the intent still reconciled).

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 design v2.1).
