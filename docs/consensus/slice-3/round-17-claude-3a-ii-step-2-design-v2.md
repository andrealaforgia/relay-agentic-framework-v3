# Slice 3a-ii, step 2, design v2: questions, answers and changed decisions (consolidated)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. No code yet. All eight findings of round 16 accepted. This document stands alone and replaces the round 16 design entirely. Round 2 of at most 3 (D-8).

## 1. Three things stop a scope, independently

A **scope** is a whole feature or one story of it. At every point where work starts, under the project's gate lock, Relay asks what stops the scope the work belongs to. There are three answers, each recorded on its own and cleared only by its own action:

1. the feature is **closed** (paused or cancelled; step 1);
2. a **question is open** over the scope;
3. something the work relies on is **not current** (section 4).

A feature-wide question or decision covers the roadmap, and every story and behaviour of the feature that exists or is created later. A story's covers that story and its behaviours. An approved story is outside every scope. Nothing is copied to subjects: scope is looked up. (S3-4.6)

Resuming a feature clears neither a question nor staleness. Answering a story's question clears neither a feature-wide question nor a pause. A start is refused naming every reason that applies, closure first. (S3-9.6)

## 2. Questions

- `ask(principal, request_id, project, feature, story | None, text, why, options)`: a worker asks. The pair (principal, request id) identifies the request: a retry after a lost reply returns the same question, and reuse for different content is refused. (S3-9.7)
- Asking takes the gate lock **exclusively**. It is refused on a cancelled feature and on an approved story.
- While open, its scope is treated as a paused feature is in step 1, by the same code: nothing starts, `claim` is refused, grants with no first effect are taken back, results of work in flight are recorded, checks that finish are `held`, commands stay in the outbox, a first human decision at a gate in scope is refused. The story slot, if held, stays held.
- It is an inbox item of kind `question`.
- Cancelling the feature closes every open question of it, with its inbox item, as `withdrawn`. A first answer arriving later is stale and owes nothing.

## 3. Answers and decisions

- A **decision** has a project, a feature, a scope (the feature, or one story), a source (`product_owner` or `model`), a text, the question it answers if any, and the decision it replaces if any. A feature's decisions are numbered in the order they commit.
- **Answer**: a human action targeting the question, under the exclusive gate lock. It is effective only if the question is still open. A second, different request for the same question gets a stale receipt naming the decision already made. Exact replay returns the original receipt. (S3-9.7)
- **Replace**: a human action targeting a decision. It is effective only if that decision is the **head** (not already replaced). The new decision takes its project, feature and scope from the old one; the caller cannot give them. Both rules are database constraints: one answer per question, one successor per decision, and a successor's (project, feature, scope) equal to its predecessor's. Replacing a non-head is stale.
- A worker may record a `model` decision. It moves nothing and stops nothing.
- `context_for(project, feature, story | None)` returns the decisions in force (heads only), each with its source, **and the basis** (section 4). It works before any story exists.
- A gate's manifest is an immutable snapshot of the decisions and assumptions in force when it opened. A new model assumption does not reopen a gate. A human decision does (section 6).

## 4. The decision basis

- A story's **basis** is the number of the latest `product_owner` decision in force over it, feature-wide or its own; zero if none. A feature's basis, for its roadmap, counts feature-wide decisions only.
- **Every role-authored submission names the basis its author read**, and the artefact versions it was written against. Relay compares under the gate lock and refuses a mismatch; it never stamps a submission with whatever is current when it arrives. This applies to `publish_roadmap`, `propose`, `register_acceptance_test`, `amend_contract`, `seal`, `assess` and `reconfirm`. (S3-9.1)
- **Every first effect records the basis it began on**, and its settlement compares: a red run, a green run, a review checkout, a check batch. A result whose recorded basis is no longer the story's is kept as evidence and changes nothing: a red or green is `basis_superseded`; a review submission on an old-basis checkout is refused, and the reviewer prepares again; a check batch is discarded as one overtaken by a new required check is today (first generation: the caller is told; later generation: the integration stays owed). (S3-9.2)
- Stored records carry a basis too: the roadmap revision, the story, each contract, the accepted red, the accepted green, the review cycle, the seal, the assessment (now unique per story revision, list revision **and basis**), the gate.
- **Current** means recorded basis equals the story's basis now.

| To do this | These must be current |
|---|---|
| Approve a roadmap; release a story of it | the roadmap |
| Propose a behaviour, begin test design, register a contract | the story |
| Run red, amend a contract | the story |
| Run green, claim a build | the story, the contract, the red |
| Claim or check out a review, submit one | the story, the contract, the red, the green, the cycle |
| Integrate, take up an integration, settle a batch into an intent | all of the above, and the batch |
| Seal, assess | the story and every contract and its evidence in the list |
| Open the story gate | all of that, the seal and the assessment |

An intent already authorised runs to its end, whatever lands. That boundary is unchanged.

## 5. Bringing things back to current

**Reconfirmation**, by the owning role, is the only way an unchanged artefact becomes current, and each kind is an assignment (section 7):

- **The roadmap** (Analyst): the stories and their order stand. If the roadmap was already approved, no new approval is asked for: the Product Owner approved this content and made this decision. If it was only proposed, reconfirmation opens a new gate whose manifest shows the decision.
- **A story** (Analyst): its text and criteria stand.
- **A contract and its evidence** (Verifier): the contract stands, and so does **every** piece of evidence the behaviour currently has. The submission names the contract's digest and the exact identifiers: the accepted red; the accepted green if there is one; the live review cycle if there is one; the integration if there is one. Relay requires that list to equal the behaviour's current records, no more and no fewer, and restamps exactly those. If a green was accepted between the Verifier reading and submitting, the list no longer matches and the submission is refused. There is no retaining some and not others: a Verifier who cannot vouch for all of it amends the contract. (S3-9.2)

Every reconfirmation is compare-and-set on the basis it names, and is made under a grant (section 7). One written against basis 2 and arriving after basis 3 exists is refused.

**Seals and assessments are never reconfirmed.** They are made again, naming the new basis.

**Amendment**, where the artefact does not stand (S3-9.4):

- **A contract**, while the behaviour is awaiting red or being implemented: `amend_contract(verifier, project, behaviour, contract, basis)` replaces the contract, ends the pairing with the old red, retires the build that was owed, and returns the behaviour to awaiting red. It is the one operation that may start in a scope stopped only for staleness.
- **A contract of a behaviour in review, integrated, or with an authorised intent**; **a story's text or criteria**; **a roadmap's content**: not in this step. These are corrections (step 3). Until step 3 exists the outcome is explicit: the artefact stays not current, the scope stays stopped, and the only ways forward are reconfirmation or cancelling the feature. Relay reports this as the reason, so the scope does not look merely idle.

## 6. What a human decision does, in its own transaction

Under the exclusive gate lock, the answer or replacement:

1. records the decision and, for an answer, closes the question and its inbox item;
2. supersedes every open gate in scope, with its inbox item. A decision sent to the old gate afterwards is stale;
3. returns every story in scope that was awaiting approval to being worked on, keeping its place, and clears its seal, so it can be sealed and assessed again on the new basis (S3-9.3);
4. takes back grants with no first effect in scope, and retires reconfirmation assignments owed for an earlier basis;
5. owes the reconfirmations of section 7;
6. evaluates gates and runs the ordered release (below).

**Release.** A planned story is released only if its feature is open, no question covers it, and its roadmap and the story itself are current. A story that fails this is not released and the stories after it in its feature wait behind it: none jumps its predecessor. If no story holds the project's place, another feature whose next story is eligible takes it. Release and gate evaluation run in every transaction that can change eligibility: answer, replace, reconfirm, resume, cancel, and settlement of an integration. The pass on start and after resume runs them as well, so a scope whose last blocker cleared just before a crash is not left waiting. (S3-9.6)

## 7. Reconfirmation as owed work (S3-9.5)

- An assignment has a typed **subject**: a behaviour, a story, or a feature's roadmap. Step 1's assignments all have a behaviour subject; nothing about them changes.
- A human decision owes, for each artefact in scope that it made not current: one `reconfirm` for the roadmap if the decision is feature-wide (Analyst); one per story not yet approved (Analyst), whether or not it has behaviours; one per behaviour that has a contract (Verifier). Nothing is owed for seals and assessments.
- These are claimed like any other, and alone may be claimed while their scope is stopped **only** for staleness. A pause or an open question stops them too.
- `reconfirm` names the grant. Its first effective completion needs the assignment's current grant, the subject's current revision (the roadmap revision; the story revision; the contract digest with the evidence list) and the target basis. A grant taken back by a pause completes nothing.
- Amending a contract completes the Verifier's assignment for that behaviour.

## 8. Tests

**Scenario 3**: a red run refused naming the question; the answer in the story's context as a `product_owner` decision; after the Analyst and the Verifier reconfirm, the run proceeds.

**S3-4.6**: ask before any roadmap exists, restart, `publish_roadmap` refused; answer; publish naming the new basis; the decision is in the first story's context. A roadmap prepared against basis 0 and submitted after the answer is refused.

**Scope, both commit orders** (S3-9.8): `publish_roadmap` racing `ask`. Publication first: the question stops the stories just committed. Asking first: publication is refused; after the answer the submission prepared on the old basis is still refused.

**S3-9.1**: an assessment prepared on basis 1 and arriving after the answer, the reconfirmations and a new seal is refused; one stored before the answer does not open the gate.

**S3-9.2 and S3-4.7**: a green held from basis 1, the contract reconfirmed with the red retained, the green released: recorded as `basis_superseded`, no cycle. A reviewer who checked out before the decision cannot submit until it prepares again. A passing batch held, the decision replaced, everything reconfirmed, the batch released: no intent; checks re-run on the new basis; one integration. A reconfirmation naming a green that is not the behaviour's current one is refused. A behaviour of another story, out of scope, keeps its evidence and integrates.

**S3-9.3**: a question asked and answered while a story awaits approval. The old approval is stale at once; the story is resealed and reassessed on the new basis; after a restart a new gate with a new manifest is approved.

**S3-9.4**: the Verifier amends a contract after an answer: fresh red required, the old build cannot be claimed. Amending a behaviour in review is refused with the reason "needs a correction". A scope stopped that way reports it.

**S3-9.5**: an answer about a planned story with no behaviours owes the Analyst one reconfirmation; no behaviour is invented. A reconfirmation under a grant a pause took back is refused. A second decision retires the first set.

**S3-9.6**: a story question answered while a feature question is open releases nothing; resuming clears neither question; cancelling closes the questions and a late answer is stale. With the first planned story of a feature stopped: a later story of that feature is not released, another feature's story is; when the blocker clears the first story is released, including when the clearing transaction is followed by a restart.

**S3-9.7**: two different requests answering one question: one decision, the second stale. Two replacing one decision: one successor. `ask` retried: one inbox item. A replacement cannot change scope.

**Locking**: ask, answer and replace each raced against a start, both orders.

## 9. Left out, on purpose

- Narrowing a question's scope. Non-blocking questions (a worker records a `model` decision instead).
- Per-decision dependencies: a human decision makes everything in its scope not current.
- Changing anything already in review, integrated or approved, and changing a story or a roadmap: step 3.
- Reviewers are not asked to reconfirm; the Verifier's reconfirmation names the cycle it keeps, and a reviewer still in flight prepares again.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 2 design v2), saying for each finding whether a real entry point can reach it.
