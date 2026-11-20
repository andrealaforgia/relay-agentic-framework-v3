# Slice 3a-ii design v1: closing gates on work that has already started

3a-i stops between stories. 3a-ii stops inside one: for a pause, a cancel, an open question or a changed decision. It also gives a rejected or changed story a way forward, and ends a feature. Scenario 3 and the failure cases around the gate. It answers S3-1.1 (the parts 3a-i did not need), S3-1.4, S3-1.5, S3-2.4 and S3-2.5.

Still no model anywhere. Roles are scripted principals.

## 1. Where "closed" lives, and one locking rule

A pause, a cancel and a blocker are all recorded **on the story row or in rows keyed by the story**, never only on the feature. The reason is the lock order (behaviour, story, project, feature): every point that authorises work already holds its story's lock, and could not look at a feature without taking locks out of order.

- **Everything that closes work takes the story lock exclusively.** Pausing or cancelling a feature locks all its stories (in story-id order), marks each `closed_by = paused | cancelled`, and then records the feature's own state.
- **Everything that authorises work holds the story lock shared** and reads `closed_by` and the story's open blockers there.

So a closure either commits first and is seen, or waits for the authorising transaction to commit.

## 2. The points that authorise work

`may_proceed(story, behaviour?)` is true when the story is being worked on (3a-i), `closed_by` is null, and no open blocker applies to the story or to that behaviour. It is asked at:

| Point | If it says no |
|---|---|
| Starting an operation (propose, test design, register, run red, run green, integrate, seal, assess) | Refused, naming the pause, the cancel or the question |
| A result that releases a successor: an accepted red (Builder command), a published candidate (review commands), a blocking finding or a failed check (remediation command) | The evidence and the state change commit. The command is stored **held** |
| The publisher | Held rows are never published |
| `claim(message_id)`, which a worker calls immediately before acting on a command it received | Refused if the command is held, discarded, or no longer current; the worker does nothing |
| Inserting an integration intent | No intent. The batch is closed as `held` with its runs kept |
| Releasing a story; opening a story gate | Not done |

The trunk boundary is unchanged from v2: the commit of the intent. Closure first, no intent. Intent first, it is reconciled to its end whatever the feature's state, and its settlement neither opens a gate nor releases anything while the story is closed.

## 3. Held commands and reassessment (S3-2.4)

Every command row records what it was issued on: the behaviour, and the red cycle, review cycle or check batch it belongs to. A command is **current** while that is still the behaviour's.

`reassess(story)` runs in the transaction of a resume, of an answer that clears the last blocker, and on start. Under the story lock, if `may_proceed`:

1. Each held command that is still current is released (the same message, the same identity). One that is not current is marked discarded.
2. For each behaviour whose latest batch is `held`, whose review cycle is still live and satisfied: exactly one `integration.requested` command is issued to the control plane, carrying a request id derived from the held batch. Calling `integrate` with that id runs the checks afresh; a repeat of the command changes nothing. If the cycle is no longer live, nothing is issued.
3. The story gate is evaluated.

Cancel discards every held command and marks held batches abandoned. Nothing is reassessed after it.

Tests: pause while the last check is held at a barrier (no intent; after resume one integration request; honoured, trunk moves once); the same with the red reopened in between (no request); pause, cancel and a question each raced against intent creation on both sides of the commit; a command already in Redis claimed after a pause (refused) and after resume (granted); an accepted red while paused (recorded, Builder command held, released by resume); restart between pause and resume.

## 4. Questions, blockers and decisions (S3-1.4)

- **Question.** Asked by a worker principal through one operation: text, why it matters, options with trade-offs, and the subjects it blocks: the analysis, stories, behaviours. It is an inbox item.
- **Blocker.** A row per (question, story) or (question, behaviour). Blocking the analysis writes one for every story of the feature not yet approved. Opening a blocker takes the story lock exclusively. Only the Product Owner may narrow a question's scope, by a human action.
- **An open story gate** whose story, or any behaviour in whose manifest, becomes blocked is superseded in the same transaction, with its inbox item; the story returns to being worked on, and the gate is evaluated again when the blocker clears, with the answer in the new manifest.
- **Answer.** A human action on the question. It creates a decision of source `product_owner`, clears the question's blockers, records the decision as a dependency of every subject it had blocked, and reassesses the stories concerned.
- **Decision.** Source `product_owner` or `model`, kept apart wherever shown. Artefacts record the decisions they rely on: a roadmap revision, a story revision, a contract (given by the author when it is made, plus the answers above). `context_for(story)` returns them with their source.
- **A replaced decision.** A human action replaces a decision with a new one. Every story and behaviour that recorded a dependency on the old one gets a blocker, "decision changed". It is cleared by the owning role reconfirming that artefact against the new decision (the Analyst for a story, the Verifier for a contract), which records the new dependency; or the artefact is corrected by the routes in section 5. Subjects with no recorded dependency are untouched and keep their evidence.

Tests: scenario 3 end to end (a blocked red run is refused naming the question; the answer is a `product_owner` decision in the story's context; the run proceeds); a question on the analysis after roadmap approval stops the running story's next step and the next story's release; a decision replaced while a dependent check is held yields no intent, while a behaviour of another story with no dependency keeps its evidence and integrates; a blocker on one behaviour supersedes its story's open gate.

## 5. Corrections (S3-1.5, S3-2.5)

- **After `request_changes`** the story is `changes_requested` at a new revision with no seal (3a-i). The Verifier adds behaviours, seals; the Analyst assesses; a new gate opens. Nothing on trunk is undone.
- **After `reject`** the story is `rejected`. The Analyst publishes a new roadmap revision for the feature: earlier stories keep their identity and state, and at least one new story is marked as correcting the rejected one. The Product Owner approves it at a roadmap gate. A rejected story with a corrective story in the approved revision no longer holds up the order; ordinary successors are placed after the corrective story.
- **Replacing an integrated behaviour.** Sometimes the correction contradicts something on trunk: behaviour X requires A, and the Product Owner now wants B. The authority for that is exact and separate from either route above:
  1. The Verifier proposes a supersession naming the old obligation (X's contract digest) and the one replacing behaviour, Z, with a reason. This opens a gate of its own, whose manifest shows X's contract and Z's summary and criteria.
  2. The Product Owner approves that gate.
  3. From then, X's obligation is left out of the regression set **only for candidates of Z**. Every other candidate, including other behaviours of the same corrective story, must still satisfy it.
  4. When Z is integrated, X's obligation is retired for everyone, in that transaction.
  5. Approval and retirement each bump the check-policy version, so a batch running across either is discarded by the existing rule. The obligation's history is kept.

Tests: change request, a new behaviour, a second gate; reject, corrective roadmap, approval, the corrective story released and the ordinary successor still waiting; a corrective story with two behaviours where only the second replaces X: the first cannot integrate while breaking X, the second can, and after it X no longer applies to anyone; without the approved supersession the second cannot integrate.

## 6. Feature completion

When the last story of the approved roadmap is approved, the feature is `checking` and one command asks the control plane to run the feature-wide checks. `complete_feature(request_id)` runs the project's regression set on the trunk head through the oracle. All satisfied: `completed`, with a stored summary (stories, limitations recorded, usage `unknown`). Otherwise the feature needs a decision and an inbox item says which checks failed.

## 7. Order of building, each step reviewed

1. Pause, resume, cancel; held commands; claim; reassessment (sections 1 to 3).
2. Questions, answers, decisions and their replacement (section 4).
3. Corrections and supersession (section 5).
4. Feature completion (section 6).

## What I would like challenged

- Recording closure on story rows to keep one lock order: is there a transition it does not cover?
- Whether an `integration.requested` command is the right durable action for a held batch (S3-2.4), given that no control-plane loop exists until 3b.
- The supersession gate as the one authority for both correction routes (S3-2.5).
- Whether four review steps is the right grain.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii design v1).
