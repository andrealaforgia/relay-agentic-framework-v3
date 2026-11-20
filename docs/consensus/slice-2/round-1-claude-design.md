# Slice 2 design, for challenge before any code

Slices 1a to 1c took fifteen review rounds because each design reached you as code. This time the design comes first. Challenge it; I will build what survives.

Slice 2 must show: QA and Sentinel independently review the same immutable candidate; unresolved blocking findings prevent completion; a new candidate makes earlier reviews stale; trunk advances once, to exactly the reviewed and checked revision, and stays put on a stale base, a failed required check, or a crash at any point. Scenarios 6, 11 and the regression half of 5. No model is involved yet: reviewers are scripted callers of the application API, as the Builder is today.

I propose two sub-slices.

## 2a. Immutable candidates, reviews and findings

**Candidate ref.** When a green is verified, in the same supervised operation and before the behaviour moves to `review`, the control plane publishes `refs/relay/candidates/<project>/<behaviour>/<attempt>` at the candidate commit, by pushing from a throwaway clone with a lease that requires the ref to be absent. If the ref exists at another commit, the green is rejected. Reviews and integration read only this ref and verify it still resolves to the recorded commit. Limitation, stated: on a plain repository nothing stops someone with write access from moving the ref; Relay detects that and refuses, it cannot prevent it.

**Review tasks.** A verified green commits, with the transition, one task per required reviewer (`qa`, `sentinel`; the set is project policy, frozen on the task at creation) and one outbox command per task to that reviewer's pool. A task is bound to (project, behaviour, candidate commit).

**Submitting a review.** `submit_review(task_id, findings)`. Under the behaviour's row lock: the task must be open and its candidate must still be the behaviour's current candidate, otherwise the submission is stored and marked stale, changing nothing. A review with no findings is an approval of that commit by that reviewer.

**Findings.** Each has a stable id (assigned by Relay), reviewer, severity (`low|medium|high|critical`), a claim, evidence, affected files, a recommended remediation, the commit it was raised on, and a status: `open`, `disputed`, `resolved`, `withdrawn`. Blocking means severity at or above the project's threshold (default `high`) and status `open` or `disputed`.
- The Builder may `dispute` a finding with evidence. It stays blocking.
- Only the reviewer role that raised a finding may mark it `resolved` (naming the commit that fixed it) or `withdrawn`. In this slice "the reviewer role" is established by which task the caller holds; real authentication is slice 3 and later.
- A finding is never deleted.

**Outcomes.** When every required task for the current candidate has a submission: if any blocking finding exists, the behaviour returns to `implementing` with the findings on the build request; the Builder publishes a new attempt, a new green creates a new candidate, and new tasks are created for it (earlier submissions are stale by construction because they name the old commit). Open findings carry over: the new reviews must resolve or withdraw each one for the gate to clear. If no blocking finding exists, the behaviour becomes eligible for integration (2b).

**Restart.** Review dispatch is driven from the task rows, not from the transition: a reconciler re-announces any open task with no submission, so a behaviour already in `review` when the process restarts, or when this slice is first deployed, still gets its reviews.

## 2b. Integration

**Required checks.** A project may register required checks: process contracts with an expectation and no baseline, run through the same container oracle. Together with the contracts of every behaviour already integrated, these are the regression set. The set is frozen when the candidate enters integration and its digest is recorded.

**Integrating one candidate.** One operation, one behaviour at a time per project (a project-level lock held only for the short conditional steps, never during a subject run):
1. Preconditions under lock: behaviour is `review`; all required reviews are current for this candidate; no blocking finding; candidate ref still resolves to the recorded commit.
2. Read trunk head. It must equal the revision the red was observed on. If trunk has moved, the base is stale: record `integration_refused_stale_base`, return the behaviour to `validating_red` in a new red cycle (a new red on the new trunk, then a new candidate and fresh reviews). Nothing is rebased automatically.
3. Run the regression set on the candidate through the oracle. Any check not satisfied, or ineligible: record which, return the behaviour to `implementing`. Trunk untouched.
4. Persist an integration intent (project, behaviour, candidate, expected trunk head) and commit.
5. Advance trunk by a push with a lease on the expected head, fast-forward only.
6. Record `integrated` with the commit, mark the intent done.

**Crash recovery.** On start, for each intent not done: read trunk. If trunk is the candidate, record `integrated` (the push happened). If trunk is the expected head, the push did not happen: re-check the preconditions and retry from step 5, or abandon the intent if they no longer hold. If trunk is anything else, record a conflict and block the behaviour for a human. Tests kill the operation after step 4 and after step 5.

**What the Builder cannot do.** The Builder still only publishes attempt refs. Advancing trunk is done by the control plane. Stated limitation, as before: on a plain repository that separation rests on credentials Relay does not manage.

## Deliberately not in slice 2

Worker leases and concurrent ownership (slice 5); authenticated reviewer identity and real QA and Sentinel agents (slice 3 onwards); deterministic scanners alongside Sentinel; mutation testing as a QA input; story-level approval.

## What I need from you

1. Any way this design lets trunk receive a commit that was not the one reviewed and checked, or lets a blocking finding be bypassed.
2. Whether returning to `validating_red` on a stale base is right, or too costly a response to an unrelated trunk change.
3. Anything here that will not survive contact with real Git or real Docker, which you would rather I learnt now.
4. Whether 2a and 2b are cut in the right place.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for the slice 2 design).
