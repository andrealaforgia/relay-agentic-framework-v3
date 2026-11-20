# Slice 3 design v2

All ten findings accepted, and your answers to my four questions adopted. This replaces v1 for 3a. 3b and 3c keep v1's outline plus the commitments in the last section, and each gets its own design round before it is built.

## The split

- **3a-i.** Two scripted stories from an approved roadmap through integration to an exact human gate, and restart with an empty broker. Scenarios 8 and 14.
- **3a-ii.** Questions, changed decisions, correction flows, pause and cancel, feature completion. Scenario 3 and the failure cases around the gate.
- **3b.** Workers and the Claude adapter. **3c.** CLI, live session, packaging. Scenario 1's final smoke starts from a problem typed into the live session and reaches story approval through the real roles, Codex disabled, with no harness call advancing a missing transition.

## Parentage (S3-1.9)

No release has been tagged and migrations are still edited in place, so there are no rows to adopt. `behaviours.story_id` becomes `NOT NULL` with a foreign key; `propose` takes a story and there is no other way to create a behaviour. Feature and story are always derived from those relationships, never passed alongside. The tests of slices 1 and 2 get a fixture that makes a feature with an approved roadmap and a released story.

One story at a time **per project**: a partial unique index over stories in `released`, `changes_requested` or `awaiting_approval`, keyed by project. Features of one project queue behind it.

## One eligibility rule (S3-1.1)

`eligible(subject)` is one function. A subject is eligible when its feature is neither paused nor cancelled, no open blocker applies to it (see blockers below), and its story is the project's current story.

It is evaluated, under locks, at **every point that authorises new work**, not only where an operation starts:

| Point | If not eligible |
|---|---|
| Starting an operation (propose, begin test design, register a contract, run red, run green, integrate) | Refused, naming the reason |
| A result that would release a successor (an accepted red releasing the Builder; a green opening review; a blocking finding or a failed check sending work back) | The evidence and the state change are committed. The command is committed too, but **held** |
| Publishing or re-announcing any command | Held rows are not published |
| A worker claiming a command it has received, immediately before acting | The claim is refused; the worker does nothing and the command stays held for re-delivery |
| Inserting an integration intent | No intent. The batch is closed as `held`, its runs kept |
| Releasing a story | Not released |

Held commands are outbox rows with a hold; the publisher skips them, and a command already in Redis is stopped by the claim. `resume`, and the answering of a question, run one **reassessment** that releases every held command that is now eligible; it is idempotent and also runs on start.

Lock order: behaviour, then story, then feature, then project. Operations that authorise work hold the feature row shared; pause, cancel and anything that opens a blocker take it exclusively. So a closure either commits first and is seen, or waits for the authorising transaction.

**The one boundary for trunk** (your answer 2): the commit of the integration intent. Closure first: no intent, no push. Intent first: it owns that exact conditional push, through restarts, and trunk may advance after the Product Owner has seen "cancelled". Status shows the outstanding intent. Its settlement records what the repository says and neither opens a gate nor releases work while the feature is closed. Pause keeps held work for reassessment; cancel makes it terminal, and late results are recorded for audit only.

Tests: pause, cancel and a blocking question each raced against intent creation, on both sides of the commit; a command already delivered to Redis claimed after closure; a red accepted while paused (recorded, Builder command held, released on resume).

## Gates and human actions (S3-1.2, S3-1.6, S3-1.8)

- **Gate.** An immutable record: id, kind (`roadmap` or `story`), the revision it concerns (roadmap revision; or story revision plus behaviour-list revision), and the digest of its evidence manifest. `open`, `decided` or `superseded`. Opening a newer gate for the same subject supersedes the older.
- **Inbox item.** Every open gate and open question is a row in PostgreSQL, written in the transaction that opens it, together with its outbox notification. The CLI and any session read the inbox from PostgreSQL. Redis only says "look".
- **Human action.** Carries a principal, a request id, the gate (or question) id and the action. The first action on an open gate takes effect and its receipt is stored. The same request id again returns that receipt. A first action against a superseded or decided gate is stored as stale and changes nothing. The same applies to roadmap approval and to answers.
- **Principals.** Every application operation takes a principal: `human`, `worker` (with a role) or `control_plane`. Product Owner operations require a human principal. 3a uses scripted principals; the interface is the real one. A worker, and therefore any model, calling a Product Owner operation is refused. PO-7 is about building Relay and gives no runtime authority over a story.

Tests (3a-i): the lost reply and the late first submission, separately; approve racing request-changes; a worker principal refused; restart with Redis flushed and the session gone, finding the same gate id and evidence digest, one effective action, no new execution.

## Stories, criteria and the story gate (S3-1.3)

- Acceptance criteria have stable ids within a story revision.
- The Verifier registers behaviours, each naming the criteria it demonstrates, and **seals** a behaviour-list revision. Sealing is refused unless the list is non-empty and every criterion is covered by at least one behaviour. A criterion can be left uncovered only by a recorded Product Owner disposition. Appending after sealing makes a new revision, which must be sealed again.
- The Analyst records an assessment of the sealed revision: whether the behaviours demonstrate the outcome. It is part of the review. Code does not judge it; code requires it to exist.
- **Gate evaluation** is one idempotent function, run on sealing, on each integration, on the Analyst's assessment, on blocker clearance and on resume. It opens the story gate when the sealed revision is the latest, every behaviour in it is integrated, the assessment exists and the story is eligible. So the order of sealing and the last integration does not matter.
- The review is assembled by code from records. Usage is `unknown` until slice 5 measures it; it is never shown as zero.

Tests (3a-i): both orders of seal and last integration; an omitted criterion; an append racing a seal.

## Corrections (S3-1.5, your answer 1)

- `request_changes(gate, reason, criteria)` opens a **new revision of the same story**, carrying the reason and the criteria concerned. It does not demand a new behaviour: the Verifier may add behaviours, or re-register a contract through the existing red path, and seals a new list revision. The integrated commits and the original review stay.
- `reject(gate, reason)` closes that story revision. Ordinary successors are not released. The Analyst must publish a roadmap revision containing a story marked as correcting the rejected one, and the Product Owner approves that revision at a roadmap gate. A rejected story with an approved corrective successor no longer blocks the order.
- **A changed behaviour.** If the Product Owner now wants B where an integrated contract requires A, the roadmap gate lists, explicitly, each regression obligation the corrective story supersedes. Approving that gate is the authority. A superseded obligation is left out of the regression set for the corrective story's own candidates, and retired for everyone when the replacing behaviour integrates. Its history is kept, every such change bumps the check-policy version, and a batch running across it is discarded by the existing rule.

Test (3a-ii): a corrective story reaches trunk while every unrelated obligation is still enforced; without the approved supersession it cannot.

## Questions, blockers and decisions (S3-1.4)

- **Dependencies are recorded.** A roadmap revision names the analysis version it derives from; a story revision its roadmap revision; a contract its story revision. Each also lists the decisions it relied on, given by its author, plus automatically the answer to any question that blocked it.
- **Blockers propagate downwards.** A question names subjects. Blocking an analysis blocks the roadmap and every story derived from it; blocking a story blocks its behaviours. Only the Product Owner may narrow a blocker's scope.
- **A superseding decision.** The Product Owner may replace a decision. Every artefact that recorded a dependency on the old one gets an automatic blocker, "decision changed". It is cleared by its owner role either reconfirming the artefact against the new decision or revising it (the Analyst for stories, the Verifier for contracts). Artefacts with no recorded dependency keep their evidence and stay eligible.
- Reassessment runs on every answer and on start.

Tests (3a-ii): scenario 3; a question on the analysis after roadmap approval stops a running story's next release; a changed answer while a dependent check is held produces no intent, and an unrelated story keeps its evidence.

## Feature completion

After the last story is approved, the project's regression set is run on the trunk head through the oracle. All satisfied: the feature is `completed` and the summary is recorded. Otherwise the feature needs a decision. (3a-ii.)

## Records settled now for 3b and 3c (your answer 4)

- **Task.** Stable id; project, feature, story, behaviour and the revisions of each it concerns; role and kind. Distinct from a delivery (the message), an attempt, and an invocation (3b).
- A task pins its input context: criterion ids, decision ids with their source, artefact revisions.
- Human actions as above. Analysis versions and every exchange with the Product Owner are persisted as they occur.

## Commitments for 3b and 3c, to be designed in their own rounds

- **S3-1.7.** Before the first real call, an invocation intent bound to task, attempt and context revision is persisted. The raw result is stored before its workflow effect is applied. A known result is recovered once; an uncertain paid call is parked for a human under D-6. A malformed reply is a known invocation, and a retry is a separately recorded call.
- **S3-1.10.** 3b is explicitly single-host. Remote workers, with a trusted transfer of the exact candidate tree and its digest, come with slice 4. Git publication and credentials stay with the supervisor; each model is scoped to its role; gate evidence stays in the local Docker oracle. D-5 falls due in 3b: capability verifiers hashed, protected re-delivery, command payloads redacted.
- **S3-1.8.** The live session may present and prepare an action. A trusted human confirmation, which the session has no means to produce, binds it to the exact gate or question, and its receipt is committed before success is reported. How that confirmation is kept out of the model's reach is the central question of the 3c design.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3 design v2).
