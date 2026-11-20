# Slice 2 design v2

All seven findings accepted, with your positions on stale bases, Git and Docker. This replaces v1.

## Records

- **Review cycle.** Created when a green is verified. Identity is its own id. It binds: the green execution, the accepted red execution, the candidate commit, the candidate ref, and a policy snapshot (required reviewer roles and the blocking severity threshold). A behaviour has at most one current cycle. Everything downstream names the cycle, never just a commit (S2-2).
- **Candidate publication.** Part of the cycle row: destination ref and commit, with a state `pending` or `published` (S2-1).
- **Review task.** One per (cycle, role), unique. Holds a secret capability generated at creation and delivered only in the command to that role's pool; the task id alone grants nothing (S2-3).
- **Submission.** One effective submission per task. A retry with identical content returns the first receipt and the same finding ids. A retry with different content is stored as a conflicting submission and changes nothing (S2-2).
- **Finding.** Stable id, behaviour, raising role, cycle raised in, severity, claim, evidence, affected files, remediation, status (`open`, `disputed`, `resolved`, `withdrawn`), and for a resolution the cycle in which it was verified.
- **Check run.** One oracle execution of one member of the regression set on one candidate, bound to a cycle (S2-7).
- **Integration intent.** One outstanding per project, unique. Binds the cycle, candidate commit, expected trunk head, the review submissions relied on, the check-policy version and every check run relied on (S2-4).

## 2a. From verified green to a decision on one review cycle

**Verified green.** In the transaction that accepts the green, under the behaviour lock and after re-checking the red pairing: create the cycle with publication `pending`, create the tasks and their outbox commands, move the behaviour to `review`. Nothing has touched Git yet.

**Publishing the candidate.** A separate, idempotent step driven from the cycle row: push the recorded commit object (fetched once, by the recorded hash, never re-resolved by name) to the candidate ref, requiring the ref to be absent. Absent: publish. Present at the same commit: mark `published`. Present at another commit: conflict, the cycle is blocked for a human. Reviewers are told to read the candidate ref; a task is not dispatchable until its cycle is `published`. A ref left by a superseded cycle has no authority because nothing reads refs except through a current cycle.

**Submitting a review.** `submit_review(task_capability, findings, resolutions)`. Under the behaviour lock: the capability must match an open task of the behaviour's current cycle. Otherwise the submission is stored as stale and changes nothing. `resolutions` lists carried findings this reviewer has verified as fixed or withdraws; a reviewer may only resolve or withdraw findings raised by its own role, and a resolution is recorded against the current cycle, meaning "verified on this candidate". There is no way to resolve a finding by naming a commit.

**The gate for a cycle.** Satisfied when every required role has an effective submission in this cycle and no finding of this behaviour is blocking. Blocking: severity at or above the cycle's frozen threshold and status `open` or `disputed`. A submission containing only non-blocking findings satisfies its role.

**Carried findings.** Findings belong to the behaviour and persist across its cycles. If a role with an open blocking finding is no longer required by policy, the finding stays blocking and can be closed only by a recorded Product Owner decision; that decision path is not built in this slice, so such a behaviour stays blocked and says why.

**Blocked gate.** When all required submissions are in and a blocker exists: one remediation request per cycle is committed (naming the findings), the behaviour returns to `implementing`, the cycle is closed as `changes_requested`. A new attempt and green create a new cycle.

**Recovery (S2-6).** Two idempotent reconciler passes: (1) every behaviour in `review` with no current cycle (a slice 1c leftover) gets a cycle built from its recorded green execution and commit, or is recorded as a blocked recovery if that commit cannot be fetched; (2) every cycle `pending` publication is published; every open task of a published cycle without a submission is re-announced.

**2a acceptance test, as you set it:** both commands delivered; QA approves; Sentinel raises a blocker; the Sentinel submission is replayed (one finding, one remediation request); a successor candidate; the old review delivered late (stale, nothing changes); Sentinel verifies the fix on the successor; gate satisfied. Plus the 1c restart case with the attempt ref moved and reconciliation run twice.

## 2b. Integration

**Required checks.** Process contracts registered on the project, each with an expectation and no baseline, stored in PostgreSQL like acceptance contracts, so a candidate cannot edit them. They must observe the product's public interface; a check that launches a candidate-owned script reporting "passed" is not independent evidence, and that judgement belongs to whoever registers the check. They run in the existing oracle profile only (read-only checkout, pinned image, no network); a check that cannot run there yields an ineligible execution and blocks. The regression set for a candidate is: the project's required checks plus the acceptance contract of every behaviour already integrated. Each registration bumps the project's check-policy version.

**Integrating.**
1. Read trunk head from the remote, outside any database lock.
2. Under the behaviour lock: behaviour is `review`, its current cycle's gate is satisfied, publication is `published`. Note the check-policy version and the regression set.
3. Stale base: if trunk is not the revision the red was observed on, transition `review → validating_red` in a new red cycle through an explicit audited transition that supersedes the cycle. Stop. If the next red run finds the new trunk already satisfies the contract, the behaviour goes to a new state, `needs_decision`, with that execution recorded, for the Product Owner; it is not sent back for another failing test.
4. Run every member of the regression set on the candidate through the oracle, outside locks, storing one check run each. Any member not satisfied or ineligible: commit one remediation request naming the check and its execution, return the behaviour to `implementing`, close the cycle. Stop.
5. Authorise, in one transaction under the project lock and the behaviour lock: re-verify everything (current cycle unchanged, red pairing, candidate, every required submission, no blocking finding, check-policy version unchanged, one eligible satisfied check run on this candidate in the cycle's runtime for every member of the set, plus the paired green), and insert the intent bound to those records. The unique constraint allows one outstanding intent per project. From this commit the intent owns the trunk update; anything decided later waits for it to settle.
6. Push the recorded local commit object to `refs/heads/<trunk>` with the explicit expected old value, after establishing locally that the candidate is a fast-forward of that value.
7. Record `integrated` with the commit and settle the intent.

**Reconciling an unsettled intent**, on start and on any retry: read trunk. Equal to the candidate: record `integrated`. Equal to the expected head: push again (the intent still owns the update). Anything else: record a conflict, block the behaviour, settle the intent as conflicted. Guarantee offered: one canonical integration record per behaviour and convergence on the authorised commit. Not offered: proof that Relay rather than another writer moved trunk. The deployment assumption, stated in the documentation, is a protected, fast-forward-only trunk that only the control plane writes.

**2b acceptance tests, as you set them:** a required check failing on a candidate whose own contract passes (trunk unchanged, one remediation request); a successful integration with failures injected just before the push, after the remote accepts but before the reply, and after the database commit but before the caller hears; duplicate calls and restarts converging on one record; a competing trunk advance; a gate invalidated while the last check is held at a barrier (zero pushes); a remote hook rejecting the push. All against a bare remote.

## Order of building

2a first, complete with its acceptance test, reviewed by you; then 2b.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2 design v2).
