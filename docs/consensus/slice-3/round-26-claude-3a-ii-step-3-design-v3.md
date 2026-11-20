# Slice 3a-ii, step 3, design v3: corrections (consolidated)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. No code yet. All ten findings of round 24 and all ten of round 25 accepted. This document stands alone and replaces both earlier designs; changes since v2 cite S3-13.n. Round 3 of 3 (D-8): what you find reachable now I fold into the build contract without a further round, and say so in the README.

## What exists, and what is missing

A change request at a story gate already works (3a-i). Four things do not exist: a second roadmap (so a roadmap sent back at its gate has no way forward today); rejecting a story; replacing something already on trunk; changing a contract once its behaviour is in review.

The step is built in two parts, each reviewed: **(i)** sections 1 to 3; **(ii)** sections 4 to 6.

## 1. Roadmap revisions (S3-4.9)

**Membership is versioned apart from story identity.** A story keeps one identity and one row. Which stories a revision contains, and in what order, is recorded per revision.

**One effective order.** A feature's effective revision is its latest approved one. Everything that walks a feature's stories in order reads that revision's membership and nothing else. Approving a later revision supersedes the earlier one in the same transaction, under the exclusive gate lock. Superseded revisions and their manifests stay readable.

**A proposal is a record of its own, apart from its gate (S3-12.3).** A feature has at most one outstanding proposal, enforced by the database, whether or not a gate is currently open on it. The proposal stores, for every story in it, exactly what is proposed: its position; whether it is kept, reworded, new or dropped; for reworded and new stories the full proposed title, outcome and criteria; for a correcting story, the story it corrects. **Nothing in a proposal touches a live story row until approval**: the effective revision goes on showing the original text.

**Each edit names what it was written against (S3-12.2).** For a story it rewords or drops, the proposal records that story's revision and its decision basis as the Analyst read them. The proposal as a whole names the feature's basis.

**Publishing.** The Analyst publishes a proposal for an open feature. It names **both** the effective revision it was written against (none, for a first roadmap) **and** the outstanding proposal it replaces (none, if there is none). Both are compared under the lock, so a draft written against an order that has since been replaced cannot arrive as a fresh proposal. (S3-13.3) Publication opens the roadmap gate. Work under the effective revision carries on meanwhile.

**What a proposal may contain**, checked at publication and again at approval:

- every story of the effective revision that is approved, rejected, or holding the project's place is in it, kept, in the same relative order;
- a planned story may be kept, reworded, moved or dropped;
- new stories may be added, each ordinary or correcting one named rejected story of this feature;
- **in the membership that would result**, every rejected story that is not resolved (section 2) has exactly one story correcting it. So a later proposal can neither drop the only correction of a rejection nor add a second one. Which story corrects which is fixed when the correcting story is created and never changes. (S3-13.2)

**At approval**, under the exclusive gate lock, all of this or none of it:

- the content rules still hold;
- for each reworded or dropped story: its revision and its basis are still what the edit named, and **no question is open over it**. A decision about that story since authorship makes the approval stale. A decision about a story the proposal does not edit does not;
- the effective revision is superseded and the proposal becomes effective; reworded stories get their new text as a new story revision, current on the story's basis; dropped stories become `withdrawn`; new stories are created;
- the reconfirmations owed for the superseded revision, for the proposal, and for stories reworded or dropped, are retired: the approved revision is their answer;
- release.

**What a decision does (S3-13.4).** The effective revision and the outstanding proposal are separate things to reconfirm, each with its own assignment. A feature-wide decision supersedes the proposal's open gate and makes both not current.

- The **effective revision** is reconfirmed as in step 2. That is what lets stories under it be released again, and it is owed whatever becomes of the proposal.
- The **proposal** can be reconfirmed, reopening its gate on the same stored content with the decision shown, **only if none of its edits is on a basis that has moved**. A proposal that rewords or drops a story whose basis moved cannot be made approvable by reconfirming it: the Analyst replaces it with a new proposal, whose edits name the new basis. Its own reconfirmation assignment is retired by the replacement.

**Sent back.** `request_changes` at a roadmap gate ends the proposal, and retires the reconfirmation owed for it if there was one. The effective revision is untouched: anything owed for it is still owed, and once it is current its stories are released as before.

**Withdrawn and rejected stories are history (S3-13.6).** No question can be asked about one, and no decision owes a reconfirmation for one.

**The gate's manifest** shows the proposal as a change to the effective revision: kept, reworded (both texts), moved, dropped, added, and what each correction corrects.

## 2. Setting a story aside (S3-1.5, S3-12.4)

- `reject` is a human action with a reason, on a story's gate **or on a story that is being worked on**. The second is how the Product Owner returns an active story whose criteria no longer hold, for instance after an answer the Analyst cannot truthfully reconfirm against. In both cases the story becomes `rejected`, for good, and is never edited afterwards.
- A gate is an immutable target. A story being worked on is not, so **a direct rejection names the story's revision and decision basis as the person saw them**, and is taken under the exclusive gate lock. If either has moved, the receipt is stale and nothing changes; an exact replay returns the original receipt. A rejection prepared for revision 1 that arrives after a change request has put revision 2 to work leaves revision 2 alone. (S3-13.5)
- What the story integrated stays on trunk and stays in the regression set. Its review, if it had one, stays readable.
- For the story's own unfinished work, rejection acts as cancelling a feature does, scoped to that story: its assignments are retired, nothing of it starts, results already on their way are kept as evidence and move nothing, checks that finish are abandoned, and an integration already authorised runs to its end.
- A rejected story does not hold the project's place.
- Rejection owes the Analyst one assignment, `correct`, on the roadmap, done when a revision containing the correction of that story is approved.

**Release, with corrections (S3-12.1).** Define a story as **resolved** when it is approved, or when it is rejected and the story that corrects it is resolved. So R rejected, C correcting R rejected, D correcting C approved: D is resolved, therefore C, therefore R.

**An unresolved rejection stops all ordinary work in its feature, wherever anything is placed (S3-13.1).**

- While the feature has an unresolved rejected story, the only story of it that may be released is a planned **correcting story whose target is unresolved**. Position does not enter into it: the correction is picked out by what it corrects, not by where the Analyst put it. No ordinary story of the feature is released, whether it sits before or after the rejection.
- When no rejection is unresolved, the ordinary rule applies: walking the effective revision in order, a planned story may be released if it is not stopped and every story before it is resolved.

So with R rejected, C correcting R and N ordinary, in any order: C is released and N waits. If C is then rejected and D is added correcting C: D is released, N still waits. When D is approved, C and R are resolved and N is released. The content rule of section 1 guarantees there is exactly one such correction to pick.

## 3. A contract in review that no longer stands (S3-12.5)

Step 2 lets the Verifier amend a contract, under the grant of the reconfirmation owed for it, while the behaviour awaits red or is being implemented. Step 3 extends that to **a behaviour in review, or parked as blocked, with no integration outstanding**:

- the live review cycle is superseded; its unsubmitted review tasks are retired; submissions already effective on it count for nothing towards any later cycle's gate; a submission arriving late for it is stored stale;
- **findings are the behaviour's, not the cycle's, and are untouched**: each stays as it stands, and only the role that raised it can settle it, on a later candidate, as today;
- the behaviour returns to awaiting red on the new contract.

**After a refused push (S3-13.9).** If an integration is outstanding, the reconfirmation cannot be claimed until it settles (step 2). If it settles as integrated, section 6 applies. If the repository refused it, the behaviour is parked as `blocked` with its cycle still satisfied, exactly as today, and the Product Owner's `retry_integration` remains the only way to try that push again. The Verifier needs no retry to amend: a parked behaviour is covered by this section, whether its cycle is still satisfied or was itself blocked by a ref conflict. Amending supersedes whatever cycle is live and returns the behaviour to awaiting red; the refused intent stays on record as it settled.

## 4. Obligations (S3-2.5)

Each thing a candidate must still satisfy is an obligation with an identity: one per required project check, one per integrated behaviour. It records its source, its contract and digest, and whether it is active or retired, and what retired it.

The regression set of a candidate is the active obligations, minus the ones this candidate is exempt from **by identity**, and only then reduced to one check per digest. If a behaviour's obligation and an independent project check ask for the same observation, exempting the first leaves the second in force.

Required project checks cannot be replaced in this slice: `require_check` only adds, and refuses a name already used. (S3-12.10)

## 5. Supersession (S3-4.8)

**Proposal.** The Verifier names the obligation X being replaced, by identity and digest, the behaviour Z that replaces it, and a reason. X must be an **active behaviour obligation of Z's project**, with no authority standing or reserved over it. Z must belong to a story being worked on, have a registered contract, not be integrated, have no integration outstanding, and have no other supersession proposed or standing: **one replacement per Z, as one per X** (S3-13.8). This opens a gate of its own kind whose manifest shows X's contract and Z's contract in full, with Z's story and its revision.

**Basis.** X's identity and digest; Z's contract digest; Z's story revision; the decision basis. The gate is stale if any of them changes before approval. **At approval the proposal's conditions are checked again as they stand** (S3-13.7): if Z has meanwhile integrated in the ordinary way, satisfying X, or has an integration outstanding that was authorised without this exemption, the approval is stale and gives no authority. An authority is never given to a Z that can no longer use it.

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
- **a seal vouches for one mapping of criteria to behaviours in force, and names it.** When a retirement changes that mapping for a story (X leaves it, in X's story and in Z's), that story's seal is cleared in the retiring transaction, as a decision clears it: a fresh seal and a fresh assessment are needed before its gate can open, even if every criterion is still covered. What the Verifier and the Analyst attested to is always what the Product Owner is shown. (S3-13.10)
- the gate's manifest shows, per criterion, the behaviours in force that demonstrate it, and separately the superseded ones with their replacements.

## 7. What must hold, added to step 2's table

| To do this | What must hold |
|---|---|
| Publish a roadmap proposal | the feature's basis, the effective revision and the outstanding proposal, each as named |
| Approve one | the content rules; each edit's story revision and basis; no question over an edited story |
| Reject a story | at its gate; or being worked on, at the revision and basis named |
| Reconfirm a proposal | none of its edits on a basis that has moved |
| Amend a contract in review or parked | as step 2, and no integration outstanding |
| Propose a supersession | Z's story and contract current; X active, with no authority over it; Z with no other |
| Approve one | its basis, and the proposal's conditions again |
| Authorise Z's integration with the exemption | the authority standing; X not reserved by another intent |
| Seal, assess, open the story gate | section 6 |

## 8. Tests

**Part (i)**

*Roadmap sent back*: `request_changes` at a roadmap gate, a new proposal, approval, the first story released.

*Revisions*: a reworded planned story keeps its identity; before approval the effective revision still shows the original text, including after a restart; a dropped story is withdrawn; a proposal that rewords or drops an approved story, or the story holding the place, is refused; one effective order after approval, the old manifest still readable.

*Edits and decisions* (S3-12.2): a proposal rewords S; a decision about S; approval is stale, and S's reconfirmation is still owed. The same with the decision about an untouched sibling: approvable. A question open over S: approval refused.

*One proposal* (S3-12.3, S3-13.4): R1 effective, R2 rewording an R1 story, a feature-wide answer, restart: R1 shows the original text, exactly one proposal is outstanding, a publication naming none is refused. R2 cannot be reconfirmed, because its edit is on a basis that moved; replaced by R3 naming the new basis, approved, applied. The same with R2 only adding a story: reconfirmed, its gate reopened with the decision shown, approved. R2 sent back instead: R1's reconfirmation is still owed, and once done its next story is released.

*Rejection and chains* (S3-12.1, S3-13.1): reject R; nothing ordinary in the feature is released, another feature's story is; a proposal without a correction is refused; with C, approved, in each of the orders `[R, N, C]` and `[N, R, C]`: C is released and N is not, including after a restart. C rejected, D added: D released, N waits, restart, D approved: N released.

*The correction of a rejection* (S3-13.2): with C planned as R's correction, a proposal dropping C is refused, and so is one adding a second correction of R.

*A delayed draft* (S3-13.3): a reorder drafted against R1; another reorder published and approved as R2; the first arrives naming R1 and no proposal: refused. Two order-only edits, so no story revision can mask it.

*Direct rejection* (S3-13.5): a rejection prepared for revision 1 arriving after a change request made revision 2: stale, revision 2 and its assignments untouched; its exact replay returns the same receipt.

*History* (S3-13.6): a story dropped by an approved revision; a later feature-wide answer owes nothing for it and no question can be asked about it.

*An active story set aside* (S3-12.4): one behaviour integrated, an answer changes a criterion, the Analyst does not reconfirm, the Product Owner rejects the story being worked on: its obligation stays in the regression set, its unfinished behaviour starts nothing, a correcting story is proposed, approved and released.

*Amending in review* (S3-12.5): Sentinel has raised a high finding, QA is still reviewing; the contract is amended: the cycle is superseded, QA's late submission changes nothing, and on the successor candidate an empty Sentinel review does not clear the finding.

**Part (ii)**

*Scope of the exemption* (S3-2.5): a corrective story with two behaviours where only the second replaces X. The first cannot integrate while breaking X; the second can; afterwards X applies to nobody. Without an approved supersession the second cannot integrate. An independent project check with X's digest still blocks Z.

*Bound to the intent* (S3-12.6): Z's push authorised, the reply lost, the decision basis changed, then recovery. Push landed: X retired once, Z active. Push refused: X active, nothing retired. In both, a second supersession of X is refused while the intent is unsettled.

*Voiding* (S3-12.9): approve X to Z, cancel Z's feature: X is in every regression set, and a second feature obtains authority over X. Z's contract amended after approval: Z cannot integrate until approved again.

*Declaration and refusal* (S3-12.7, S3-13.9): a decision during an outstanding push; the reconfirmation cannot be claimed; the push lands: a fresh Verifier declares the behaviour to be replaced, naming its obligation. A real hook refusal instead: the behaviour is parked as blocked with its cycle satisfied; with no retry, the Verifier amends the contract, the cycle is superseded and a fresh red is required.

*A late approval* (S3-13.7): Z integrates in the ordinary way while its supersession waits at the gate; the approval is stale and X is free for another proposal. The same while Z's ordinary push is outstanding.

*One replacement per behaviour* (S3-13.8): X1 to Z approved; a proposal of X2 to Z is refused.

*What a story demonstrates* (S3-12.8, S3-13.10): X superseded by Z demonstrating the same criterion: sealed, assessed, approved by the Product Owner, the manifest showing X as history. Z demonstrating a different criterion: sealing refused naming the unsupported criterion. The story sealed and assessed while Z is still being built, both X and Z demonstrating one criterion; Z integrates and retires X: no gate opens until it is sealed and assessed again, though the criterion was covered throughout.

*Locking and restart*: approval of a proposal raced against a release and against a rejection, both orders; restart between a supersession's approval and Z's integration.

## 9. Left out, on purpose

- Editing or removing an approved or rejected story. They are corrected by a new story.
- Reverting commits. A revert is a corrective story whose behaviours supersede what they undo.
- More than one outstanding proposal per feature; more than one standing or reserved supersession per obligation.
- Replacing a required project check.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 design v2), saying for each finding whether a real entry point can reach it, and whether part (i) can be built now.
