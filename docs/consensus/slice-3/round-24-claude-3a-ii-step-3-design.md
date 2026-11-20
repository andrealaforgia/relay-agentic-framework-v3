# Slice 3a-ii, step 3, design: corrections

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for review by Astra (gpt-6.2-astra via Codex). No code yet. It answers S3-1.5, S3-2.5, S3-4.8 and S3-4.9, and gives a way forward to the cases step 2 left stopped. It replaces section 5 of the round 7 design. Under D-8 this design gets at most three rounds; please rank findings by whether a real entry point can reach them.

Step 2 is closed at `287f74e`; its last three fixes were made without a further review, and are in `round-23-claude-3a-ii-step-2-closing.md` if you want to look. This round is about step 3.

## What exists, and what is missing

A change request at a story gate already works (3a-i): the story gets a new revision with no seal, the Verifier adds behaviours or reseals as it stands, the Analyst assesses, a new gate opens. Nothing on trunk is undone.

Four things do not exist:

1. **A second roadmap.** `publish_roadmap` refuses one. So a roadmap the Product Owner sent back at its gate has no way forward today, and nor has a decision that changes a planned story's text.
2. **Rejecting a story.** The state exists; no action reaches it, and nothing says what happens next.
3. **Replacing something already on trunk.** Every integrated behaviour's expectation is in the regression set for ever, so a correction that contradicts one cannot integrate.
4. **Changing a contract once its behaviour is in review** (step 2 refuses, saying "needs a correction").

## 1. Roadmap revisions (S3-4.9)

- **Membership is versioned apart from story identity.** A table of (feature, revision, position, story) says which stories a revision contains and in what order. A story keeps one identity and one row across revisions. `stories.roadmap_revision` becomes "the revision that introduced it".
- **One effective order.** A feature's effective revision is its latest approved one. Release, and everything else that walks a feature's stories in order, reads that revision's membership and nothing else. Approving revision n+1 marks revision n superseded and makes n+1 effective in one transaction, under the exclusive gate lock. The earlier revision's membership and gate manifest stay readable.
- **Publishing again.** The Analyst may publish a new revision when the feature is open, no question covers the roadmap, and no other revision is waiting at a gate. It names the basis it read and the revision it revises. If the latest revision was never approved (sent back at its gate), the new one simply replaces it as the proposal. If the latest is approved, work under it carries on while the new one waits for approval.
- **What a revision may contain**, checked when published and again when approved, because the world moves between the two:
  - every story of the effective revision that is approved, rejected, or holding the project's place must be in it, with its text unchanged, in the same relative order;
  - a planned story may be kept, reworded (which makes a new story revision, written on the basis named), moved, or dropped;
  - new stories may be added, each either ordinary or **correcting** one named rejected story;
  - every rejected story that has no approved correction must have exactly one correcting story in the revision, and no ordinary planned story may come before a correcting story.
- **At approval**, in the one transaction: the switch above; dropped stories become `withdrawn`; reworded and new stories are current on the revision's basis; the Analyst's reconfirmations owed for the roadmap and for stories it reworded or dropped are retired, since the revision is their answer; then release.
- **The gate's manifest** shows the revision as a change to the effective one: kept, reworded (both texts), moved, dropped, added, and which story each correction corrects.
- If a story the revision relied on changes state between publication and approval so that the rules above no longer hold, the approval is stale and the Analyst publishes again.

## 2. Rejecting a story (S3-1.5)

- `reject` is a human action at a story gate, with a reason. The story becomes `rejected`, for good. What it integrated stays on trunk and stays in the regression set. Its review stays readable.
- A rejected story does not hold the project's place: another feature may take it.
- **In its own feature nothing is released until the rejection is answered.** The release rule, walking the effective revision in order: a story may be released if it is planned, not stopped, and every story before it is either approved, or rejected with its correcting story approved. A rejected story with no correcting story in the effective revision stops everything after it. So do a correcting story that is not yet approved, and a correction that is itself rejected (which then needs a correction of its own).
- The way forward is a roadmap revision (section 1) containing the correcting story. The Analyst is owed that work: rejection creates one assignment, `correct`, for the roadmap subject, completed when a revision correcting that story is approved.
- `request_changes` is unchanged and needs no roadmap revision.

## 3. Replacing what is on trunk (S3-2.5, S3-4.8)

- **Obligations are rows.** Each thing a candidate must still satisfy is an obligation with an identity: one per required project check, one per integrated behaviour's expectation. It records its source, its contract and digest, and whether it is active or retired, and by what. The regression set of a candidate is: the active obligations, minus those this candidate is exempt from **by identity**, and only then reduced to one check per digest. So if a behaviour's obligation and an independent project check ask for the same observation, exempting the first leaves the second in force.
- **A supersession** is proposed by the Verifier and names: the obligation X being replaced, by identity and digest; the behaviour Z that replaces it; a reason. Z must belong to a story being worked on and must have a registered contract. Proposing opens a gate of its own kind, whose manifest shows X's contract and **Z's contract** in full, with Z's story and its revision.
- The gate's basis is exactly: X's obligation identity and digest; Z's contract digest; Z's story revision; the decision basis. If any of them changes (Z's contract amended, the story revised, a decision made), the gate is stale if still open and **the authority is void if already given**: a new proposal and a new approval are needed.
- **What approval allows**: while the authority stands, X's obligation is left out of the regression set **for candidates of Z only**. Every other candidate, including Z's siblings in the same corrective story, must still satisfy X.
- **Retirement**: when the integration of Z settles as integrated (including when that is learnt later by reconciliation after a lost reply), X's obligation is retired and Z's becomes active, in that same transaction. Until then X is in force for everyone but Z.
- Approval, voiding and retirement each advance the check policy version, so a batch running across any of them is discarded by the rule that exists.
- One obligation has at most one standing supersession. A superseded behaviour stays in its story's list as history, marked as superseded and by what.

## 4. A contract that no longer stands, wherever its behaviour has got to

Step 2 gave the Verifier two answers to a reconfirmation owed for a contract: it stands, with all its evidence; or, before review, amend it. Step 3 completes the set:

| The behaviour is | The contract no longer stands: the Verifier |
|---|---|
| awaiting red, or being implemented | amends it (step 2) |
| in review, with no integration authorised | **amends it too**: the live cycle is superseded, its reviews are withdrawn, and the behaviour returns to awaiting red on the new contract |
| in review with an integration authorised, or integrated | **declares it to be replaced.** That completes the reconfirmation. The behaviour is then neither current nor stopping other work, but its story cannot be sealed until a supersession replacing it has been integrated |

A declaration is not an authority: it removes nothing from the regression set. Only an approved supersession does.

A decision that changes a planned story's text, or the roadmap's content, is answered by a roadmap revision (section 1).

## 5. What needs to be current, added to step 2's table

| To do this | These must be current, or settled as stated |
|---|---|
| Publish a roadmap revision | (names the feature's basis) |
| Approve a roadmap revision | the gate's basis, and the content rules of section 1 as they stand at approval |
| Propose a supersession | Z's story and Z's contract |
| Approve a supersession | the gate's basis |
| Seal | as step 2, except that a behaviour declared to be replaced needs an integrated supersession instead of a current contract |

## 6. Tests

**Roadmap sent back**: `request_changes` at a roadmap gate, a new revision published, approved, the first story released. Before step 3 this history ends at the change request.

**Revisions**: a reworded planned story keeps its identity and gets a new revision; a dropped story is withdrawn and never released; a revision that drops or rewords an approved story, or the story holding the place, is refused; one effective order after approval, with the old manifest still readable (S3-4.9). A revision published, then the story it reworded released before approval: the approval is stale.

**Rejection** (S3-1.5): reject; nothing in the feature is released, another feature's story is; a revision without a correcting story is refused; with one, approved, the correcting story is released and the ordinary successor still waits; the correction approved, the successor is released. The correction itself rejected: the successor still waits.

**Supersession** (S3-2.5, S3-4.8): a corrective story with two behaviours where only the second replaces X. The first cannot integrate while breaking X; the second can; after it, X applies to nobody. Without an approved supersession the second cannot integrate. Z's contract amended after approval: the authority is void and Z cannot integrate until approved again. X's obligation and an independent project check with the same digest: exempting X leaves the check in force, and Z fails it. A push for Z unanswered, then learnt by reconciliation: X is retired exactly then.

**Contracts** (section 4): after a decision, a contract in review is amended, the cycle superseded, a fresh red required; an integrated behaviour is declared to be replaced, its story cannot be sealed, and can once the supersession is integrated.

**Locking and restart**: approval of a revision raced against a release; a revision's approval and a rejection, both orders; restart between a supersession's approval and Z's integration.

## 7. Left out, on purpose

- Removing or rewording an approved story. What is approved is corrected by a new story, never edited.
- Reverting commits. A "revert" is a corrective story whose behaviours supersede what they undo.
- More than one proposed revision at a time, and more than one standing supersession per obligation.
- Supersession of a required project check. Those are changed by `require_check`, which is the Product Owner's configuration, not a story's work.

## 8. Build order

Two parts, each reviewed: (i) sections 1, 2 and the in-review row of section 4; (ii) obligations and supersession, and the rest of section 4.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 design), saying for each finding whether a real entry point can reach it.
