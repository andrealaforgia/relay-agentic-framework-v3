# Slice 3a-ii, step 2, design: questions, answers and changed decisions

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for review by Astra (gpt-6.2-astra via Codex). No code yet. It answers S3-4.6 and S3-4.7 and replaces section 4 of the round 7 design. Under D-8 this design gets at most three rounds; please rank findings by whether a real entry point can reach them.

Step 1 is closed at `dc6e0ae`; its last fixes (round 14's three findings) were made without a further review, as D-8 provides. They are in `round-15-claude-3a-ii-step-1-closing.md` if you want to look, but this round is about step 2.

## The idea

Round 7 copied a blocker row onto each subject a question touched, and tracked which artefact depended on which decision. Both failed for the same reason: something created later, or something nobody had listed, was missed. This design has no per-subject rows and no dependency graph. It has two things, both read under the gate lock at every point where work starts:

1. **An open question stops its scope**, by being looked up, not copied.
2. **Every artefact records the decision basis it was made on**, a number. When a human decision lands, the number moves, and whatever was made on the old number has to be reconfirmed by the role that owns it before work resumes.

## 1. Questions

- A **question** belongs to a feature. Its scope is the whole feature or one story of it. It records who asked (a worker principal), the text, why it matters, the options with their trade-offs. It is an inbox item of kind `question`.
- Asking takes the project's gate lock **exclusively**, like a pause, and for the same reason: either the question commits first and every later start sees it, or it waits for work already admitted.
- While a question is open, its scope is treated exactly as a paused feature is in step 1, by the same code path: nothing starts (every operation in step 1's list, and `claim`), grants with no first effect are taken back, results of work in flight are recorded, checks that finish are `held`, commands stay in the outbox. The refusal names the question.
- "Its scope" is computed when asked about, not stored: a feature-wide question covers the feature's roadmap publication and approval, and every story and behaviour of the feature that exists **or is created later**; a story's question covers that story and its behaviours. So a question asked in discovery, before any roadmap, refuses `publish_roadmap`; a story added by a later roadmap revision is covered the moment it exists. (S3-4.6)
- An approved story is outside every scope: its work is finished. Changing it is a correction (step 3).
- Other features of the project are not stopped, but, as with pause, the story slot stays held.

## 2. Answers and decisions

- A **decision** belongs to a feature, has the same kind of scope, a source (`product_owner` or `model`), a text, the question it answers if any, and the decision it replaces if any. Decisions of a feature are numbered in the order they are recorded.
- The Product Owner **answers** a question by a human action targeting it (receipts, exact replay and conflicting reuse as for every human action, with the project in the identity). The answer creates a `product_owner` decision and closes the question, in one transaction under the exclusive gate lock.
- The Product Owner may **replace** any decision, including a model's, by a human action targeting it. That creates a new `product_owner` decision that names the old one.
- A worker may record a `model` decision (an assumption it made). It is kept apart from the Product Owner's wherever shown. It does not move the basis and stops nothing: it is there so the gate can show it and the Product Owner can replace it.
- `Stories.context_for(project, story)` returns the decisions in force for that story (feature-wide ones and its own, replaced ones left out but reachable), each with its source. Gate manifests show the same list.

## 3. The decision basis (S3-4.7)

- A story's **decision basis** is the number of the latest `product_owner` decision in force over it (feature-wide, or scoped to that story); zero if none. A feature's basis, for its roadmap, counts feature-wide decisions only.
- These record the basis they were made on: a roadmap revision; a story (its text and criteria, as last confirmed by the Analyst); a behaviour's contract; a seal; an assessment; a check batch; a gate (in its stored basis).
- **Current** means: recorded basis equals the story's basis now. A human decision in scope therefore makes everything in that scope not current, at once, with nothing to copy.
- **What needs to be current, checked under the gate lock where the work starts:**

| To do this | These must be current |
|---|---|
| Publish or approve a roadmap | (publishing records the feature's basis; approval needs the gate's basis to equal it) |
| Propose a behaviour, begin test design, register a contract | the story |
| Run red, run green, claim a build or a review, integrate, take up an integration | the story and the behaviour's contract |
| Seal, assess | the story and every contract in the list |
| Open the story gate | the story, every contract, the seal and the assessment |
| Settle a check batch into an intent | the batch (its recorded basis equals the story's now), plus the above |

- A batch that settles on an old basis is discarded exactly as a batch overtaken by a new required check is today: a first generation tells its caller; a later generation leaves the integration owed, and the successor runs on the new basis. A held batch released after replacement **and** clearance therefore authorises nothing. An assessment on an old basis does not open a gate; an open gate on an old basis is stale and is superseded when next evaluated, as a gate whose story changed is today.
- An intent already authorised runs to its end, as under pause. That is the one boundary, unchanged.

## 4. Reconfirmation

- The owning role brings its artefact to the current basis: the Analyst for a story, the Verifier for a contract. `reconfirm(principal, subject, *, on_basis, retains)` is compare-and-set: it names the basis it was written against, and is refused if the story's basis is no longer that number. A reconfirmation delayed until after a further replacement is refused.
- `retains` is explicit. For a story: the Analyst states that the text and criteria stand. For a contract: the Verifier states that the contract stands **and** names the evidence kept (the red, and, if they exist, the accepted green, the satisfied review cycle, the integration). The statement and the names are recorded and shown at the story gate.
- If the artefact does not stand, the owning role changes it by the route that already exists while the scope is otherwise stopped: a contract not yet past red is registered again; a behaviour being implemented has its red reopened by the Verifier; a story's text is revised by a new roadmap revision. Each records the new basis. Changing a behaviour that is in review or integrated, or a story that is approved, is a correction and waits for step 3; until then such a behaviour can only be reconfirmed with its evidence retained, or its feature cancelled.
- Seals and assessments are never reconfirmed: they are made again on the new basis.
- Reconfirmation is owed work. A human decision creates one assignment per artefact that is no longer current (kind `reconfirm`, for the analyst or the verifier), announced and claimed like any other; completing the reconfirmation completes it. These assignments, alone, may be claimed and done while their scope is stopped for being not current. A later decision retires the ones not yet done and owes new ones.
- When the last artefact in a scope is current again and no question is open, the same pass that follows a resume runs: held checks are handed on, wanted work is announced again.

## 5. What a question or decision does to things in flight

| In flight when it lands | Question open | Decision made (basis moves) |
|---|---|---|
| A red or green run | Judged; what it makes owed waits | Judged against the contract as it was; the behaviour is then not current until the Verifier reconfirms or changes it |
| A review with its checkout | May finish | May finish; the cycle's standing is part of what the Verifier names when reconfirming |
| Regression checks | `held` | Discarded as on an old basis; re-run after reconfirmation |
| An authorised intent | Runs to its end | Runs to its end |
| An open roadmap or story gate | A first decision at it is refused, naming the question | Stale; superseded and opened again when everything is current |

## 6. Tests

Scenario 3 end to end: a blocked red run is refused naming the question; the answer appears as a `product_owner` decision in the story's context; after reconfirmation the run proceeds.

S3-4.6: ask before any roadmap exists, restart, `publish_roadmap` refused; answer; publish; the decision is in the first story's context. A feature-wide question asked after approval stops the running story's next step and the next story's release. A story added by a later roadmap revision while a feature-wide question is open is covered.

S3-4.7: a passing batch held, the decision replaced, story and contract reconfirmed, the batch released: no intent, checks re-run on the new basis, one integration. An assessment made before an answer does not open the gate after it. A reconfirmation written against basis 2 and delivered after basis 3 exists is refused. A behaviour of another story, out of scope, keeps its evidence and integrates.

Locking: asking, answering and replacing each raced against a start on both sides of the commit, waiting on PostgreSQL's report of the lock wait.

Human actions: an answer replayed returns its receipt; the same request id in another project is refused; a worker principal cannot answer or replace.

Reconfirmation assignments: a decision owes them; they can be claimed while ordinary claims in the scope are refused; a second decision retires the first set.

## 7. Left out, on purpose

- Narrowing a question's scope after it is asked. Scope is fixed when asked; the Product Owner answers it.
- Non-blocking questions. Every question stops its scope. A worker that only wants to note an assumption records a `model` decision.
- Per-decision dependencies. A human decision in scope makes everything in scope not current, including artefacts it has nothing to do with. This costs the owning roles a reconfirmation and cannot miss anything.
- Reviewers are not asked to reconfirm. The Verifier's statement of retained evidence covers the cycle, and is shown at the gate.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 2 design), saying for each finding whether a real entry point can reach it.
