# Slice 3a-ii, step 3, design v2: corrections (consolidated)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. No code yet. All ten findings of round 24 accepted. This document stands alone and replaces the round 24 design entirely; each change cites its finding. Round 2 of at most 3 (D-8).

## What exists, and what is missing

A change request at a story gate already works (3a-i). Four things do not exist: a second roadmap (so a roadmap sent back at its gate has no way forward today); rejecting a story; replacing something already on trunk; changing a contract once its behaviour is in review.

The step is built in two parts, each reviewed: **(i)** sections 1 to 3; **(ii)** sections 4 to 6.

## 1. Roadmap revisions (S3-4.9)

**Membership is versioned apart from story identity.** A story keeps one identity and one row. Which stories a revision contains, and in what order, is recorded per revision.

**One effective order.** A feature's effective revision is its latest approved one. Everything that walks a feature's stories in order reads that revision's membership and nothing else. Approving a later revision supersedes the earlier one in the same transaction, under the exclusive gate lock. Superseded revisions and their manifests stay readable.

**A proposal is a record of its own, apart from its gate (S3-12.3).** A feature has at most one outstanding proposal, enforced by the database, whether or not a gate is currently open on it. The proposal stores, for every story in it, exactly what is proposed: its position; whether it is kept, reworded, new or dropped; for reworded and new stories the full proposed title, outcome and criteria; for a correcting story, the story it corrects. **Nothing in a proposal touches a live story row until approval**: the effective revision goes on showing the original text.

**Each edit names what it was written against (S3-12.2).** For a story it rewords or drops, the proposal records that story's revision and its decision basis as the Analyst read them. The proposal as a whole names the feature's basis.

**Publishing.** The Analyst publishes a proposal for an open feature. If a proposal is outstanding, the new one must name it and replaces it; otherwise it must name none. Publication opens the roadmap gate. Work under the effective revision carries on meanwhile.

**What a proposal may contain**, checked at publication and again at approval:

- every story of the effective revision that is approved, rejected, or holding the project's place is in it, kept, in the same relative order;
- a planned story may be kept, reworded, moved or dropped;
- new stories may be added, each ordinary or correcting one named rejected story of this feature;
- every rejected story with no correcting story in the effective revision has exactly one in the proposal.

**At approval**, under the exclusive gate lock, all of this or none of it:

- the content rules still hold;
- for each reworded or dropped story: its revision and its basis are still what the edit named, and **no question is open over it**. A decision about that story since authorship makes the approval stale. A decision about a story the proposal does not edit does not;
- the effective revision is superseded and the proposal becomes effective; reworded stories get their new text as a new story revision, current on the story's basis; dropped stories become `withdrawn`; new stories are created;
- the Analyst's reconfirmations owed for the roadmap, and for stories reworded or dropped, are retired: the approved revision is their answer;
- release.

**What a decision does to a proposal.** A feature-wide decision supersedes the open gate, as step 2 does, and the proposal stays outstanding and not current. The Analyst then either reconfirms it (the gate opens again showing the same stored proposal and the decision) or replaces it. Nothing can be published in between without naming it, so there is never a third.

**Sent back.** `request_changes` at a roadmap gate marks the proposal sent back, which ends it: the Analyst publishes a new one naming none.

**The gate's manifest** shows the proposal as a change to the effective revision: kept, reworded (both texts), moved, dropped, added, and what each correction corrects.

## 2. Setting a story aside (S3-1.5, S3-12.4)

- `reject` is a human action with a reason, on a story's gate **or on a story that is being worked on**. The second is how the Product Owner returns an active story whose criteria no longer hold, for instance after an answer the Analyst cannot truthfully reconfirm against. In both cases the story becomes `rejected`, for good, and is never edited afterwards.
- What the story integrated stays on trunk and stays in the regression set. Its review, if it had one, stays readable.
- For the story's own unfinished work, rejection acts as cancelling a feature does, scoped to that story: its assignments are retired, nothing of it starts, results already on their way are kept as evidence and move nothing, checks that finish are abandoned, and an integration already authorised runs to its end.
- A rejected story does not hold the project's place.
- Rejection owes the Analyst one assignment, `correct`, on the roadmap, done when a revision containing the correction of that story is approved.

**Release, with corrections (S3-12.1).** Define a story as **resolved** when it is approved, or when it is rejected and the story that corrects it is resolved. So R rejected, C correcting R rejected, D correcting C approved: D is resolved, therefore C, therefore R.

Walking the effective revision in order, a planned story may be released if it is not stopped and every story before it is resolved, **except that a correcting story is not held up by the story it corrects, nor by that story's own rejected predecessors in the same chain**. An ordinary story is held up by any unresolved story before it.

So with `[R rejected, C corrects R, N ordinary]`: C is released at once; N waits. If C is then rejected and D is added correcting C: D is released; N still waits. When D is approved, R and C are resolved and N is released. Where the Analyst places a correction relative to what it corrects does not matter; ordinary successors wait until the chain ends in an approval.

## 3. A contract in review that no longer stands (S3-12.5)

Step 2 lets the Verifier amend a contract, under the grant of the reconfirmation owed for it, while the behaviour awaits red or is being implemented. Step 3 extends that to **a behaviour in review with no integration authorised**:

- the live review cycle is superseded; its unsubmitted review tasks are retired; submissions already effective on it count for nothing towards any later cycle's gate; a submission arriving late for it is stored stale;
- **findings are the behaviour's, not the cycle's, and are untouched**: each stays as it stands, and only the role that raised it can settle it, on a later candidate, as today;
- the behaviour returns to awaiting red on the new contract.

If an integration is authorised, the reconfirmation cannot be claimed until it settles (step 2). Then the behaviour is either integrated (section 5) or back in review or reopened (this section, or step 2's).

## 4. Obligations (S3-2.5)

Each thing a candidate must still satisfy is an obligation with an identity: one per required project check, one per integrated behaviour. It records its source, its contract and digest, and whether it is active or retired, and what retired it.

The regression set of a candidate is the active obligations, minus the ones this candidate is exempt from **by identity**, and only then reduced to one check per digest. If a behaviour's obligation and an independent project check ask for the same observation, exempting the first leaves the second in force.

Required project checks cannot be replaced in this slice: `require_check` only adds, and refuses a name already used. (S3-12.10)

## 5. Supersession (S3-4.8)

**Proposal.** The Verifier names the obligation X being replaced, by identity and digest, the behaviour Z that replaces it, and a reason. X must be an **active behaviour obligation of Z's project**. Z must belong to a story being worked on, have a registered contract, and not yet be integrated. This opens a gate of its own kind whose manifest shows X's contract and Z's contract in full, with Z's story and its revision.

**Basis.** X's identity and digest; Z's contract digest; Z's story revision; the decision basis. The gate is stale if any of them changes before approval.

**Authority.** Approval gives a standing authority: X's obligation is left out of the regression set for candidates of Z only. Z's siblings must still satisfy X. An obligation has at most one authority that is standing or reserved (below).

**The authority is voided, leaving X active, when used by no intent and any of these happens (S3-12.9):** its basis changes; Z's contract is amended; Z's story is rejected; Z's feature is cancelled. Voiding frees X for another proposal, from this feature or any other in the project.

**An integration binds what it used (S3-12.6).** When an integration of Z is authorised, the intent records the exact obligation it was exempted from and the obligation Z will contribute. From that moment X is **reserved** to that intent: no other supersession of X can be proposed, approved or used, and a later decision or cancellation cannot change what this intent will do. Such an event only stops the authority being used again.

- If the intent settles as integrated, whenever and however that is learnt: X is retired and Z's obligation becomes active, in that transaction, from what the intent recorded.
- If it settles as not integrated: X stays active, the reservation ends, and the authority, if it was not voided meanwhile, stands as before.

Approval, voiding, reservation and retirement each advance the check policy version, so a batch running across any of them is discarded by the existing rule.

## 6. What a story demonstrates, and an integrated contract that no longer stands (S3-12.7, S3-12.8)

**Declared to be replaced.** After a decision, the Verifier's reconfirmation owed for an integrated behaviour has a second answer: its contract does not stand and will be replaced. That completes the reconfirmation and names the behaviour's obligation by identity. It is not an authority and removes nothing from the regression set. It can only be made about a behaviour that is integrated; while an integration of it is outstanding the reconfirmation cannot be claimed, and if the push ends refused the behaviour is back in review, where section 3 applies.

**In force.** A listed behaviour is in force unless its obligation has been retired by a supersession. A superseded behaviour stays in its story's list as history, with what replaced it.

**One rule for sealing, assessing and the gate.** All three use the same view of a story:

- every acceptance criterion must be demonstrated by at least one behaviour that is in force. A superseded behaviour demonstrates nothing: if X alone demonstrated a criterion and Z demonstrates a different one, the criterion is unsupported and the story cannot be sealed until something in force demonstrates it;
- every behaviour in force must be current; a behaviour declared to be replaced and not yet superseded is neither, so the story cannot be sealed, assessed or put to the Product Owner until its replacement is integrated;
- the gate's manifest shows, per criterion, the behaviours in force that demonstrate it, and separately the superseded ones with their replacements.

## 7. What must hold, added to step 2's table

| To do this | What must hold |
|---|---|
| Publish a roadmap proposal | the feature's basis as named; it names the outstanding proposal, or none if there is none |
| Approve one | the content rules; each edit's story revision and basis; no question over an edited story |
| Reject a story | the story is at its gate, or being worked on |
| Amend a contract in review | as step 2, and no integration authorised |
| Propose a supersession | Z's story and contract current; X active and not reserved |
| Approve one | its basis |
| Authorise Z's integration with the exemption | the authority standing; X not reserved by another intent |
| Seal, assess, open the story gate | section 6 |

## 8. Tests

**Part (i)**

*Roadmap sent back*: `request_changes` at a roadmap gate, a new proposal, approval, the first story released.

*Revisions*: a reworded planned story keeps its identity; before approval the effective revision still shows the original text, including after a restart; a dropped story is withdrawn; a proposal that rewords or drops an approved story, or the story holding the place, is refused; one effective order after approval, the old manifest still readable.

*Edits and decisions* (S3-12.2): a proposal rewords S; a decision about S; approval is stale, and S's reconfirmation is still owed. The same with the decision about an untouched sibling: approvable. A question open over S: approval refused.

*One proposal* (S3-12.3): R1 effective, R2 rewording an R1 story, a feature-wide answer, restart: R1 shows the original text, exactly one proposal is outstanding, a publication naming none is refused, reconfirmation reopens the gate on the same stored proposal with the decision shown.

*Rejection and chains* (S3-12.1): reject R; nothing ordinary in the feature is released, another feature's story is; a proposal without a correction is refused; with C, approved: C is released, N waits. C rejected, D added: D released, N waits, restart, D approved: N released.

*An active story set aside* (S3-12.4): one behaviour integrated, an answer changes a criterion, the Analyst does not reconfirm, the Product Owner rejects the story being worked on: its obligation stays in the regression set, its unfinished behaviour starts nothing, a correcting story is proposed, approved and released.

*Amending in review* (S3-12.5): Sentinel has raised a high finding, QA is still reviewing; the contract is amended: the cycle is superseded, QA's late submission changes nothing, and on the successor candidate an empty Sentinel review does not clear the finding.

**Part (ii)**

*Scope of the exemption* (S3-2.5): a corrective story with two behaviours where only the second replaces X. The first cannot integrate while breaking X; the second can; afterwards X applies to nobody. Without an approved supersession the second cannot integrate. An independent project check with X's digest still blocks Z.

*Bound to the intent* (S3-12.6): Z's push authorised, the reply lost, the decision basis changed, then recovery. Push landed: X retired once, Z active. Push refused: X active, nothing retired. In both, a second supersession of X is refused while the intent is unsettled.

*Voiding* (S3-12.9): approve X to Z, cancel Z's feature: X is in every regression set, and a second feature obtains authority over X. Z's contract amended after approval: Z cannot integrate until approved again.

*Declaration* (S3-12.7): a decision during an outstanding push; the reconfirmation cannot be claimed; the push lands: a fresh Verifier declares the behaviour to be replaced, naming its obligation. The push refused instead: the same Verifier amends the contract in review.

*What a story demonstrates* (S3-12.8): X superseded by Z demonstrating the same criterion: sealed, assessed, approved by the Product Owner, the manifest showing X as history. Z demonstrating a different criterion: sealing refused naming the unsupported criterion.

*Locking and restart*: approval of a proposal raced against a release and against a rejection, both orders; restart between a supersession's approval and Z's integration.

## 9. Left out, on purpose

- Editing or removing an approved or rejected story. They are corrected by a new story.
- Reverting commits. A revert is a corrective story whose behaviours supersede what they undo.
- More than one outstanding proposal per feature; more than one standing or reserved supersession per obligation.
- Replacing a required project check.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 design v2), saying for each finding whether a real entry point can reach it, and whether part (i) can be built now.
