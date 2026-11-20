# Consensus record: slice 3 (stories, the Product Owner's gate, real agents)

## Design, before any code

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 10 blocking | Gates checked only when work starts, not when results arrive or messages are redelivered; a late approval landing on a newer version; "list complete" both forgeable and missable; no effect defined for a changed decision; rejection with no way forward; a gate that lived only in Redis; provider calls not safe to repeat; a model able to exercise the Product Owner's authority; behaviours with no story; workers assuming local paths |
| 2 | CHALLENGE, 3 blocking for 3a-i | "Current story" wrong on both sides of the gate; a gate not stale when its basis changes; a reused request id acknowledging something never done |
| 3 | ACCEPT | Nothing new |

Design v2 with the v2.1 amendments is the basis for 3a-i. Two findings (S3-2.4 held batches on resume, S3-2.5 the scope of a regression exemption) are accepted as conditions of the 3a-ii design round. S3-1.7 and S3-1.10 are conditions of 3b, and S3-1.8 (a human confirmation the model cannot produce) of 3c.

## Slice 3a-i as built

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 9 blocking | Two deadlocks from opposite lock orders; a late assessment certifying a newer list; the roadmap shown differing from the one released; the review not saying how findings were settled; a finding changing behind an open gate; a payload overwriting the decision record; a production switch for behaviour order added for the tests' sake; an old seal surviving a change request |
| 2 | CHALLENGE, 2 blocking | A behaviour with no criterion reaching trunk and stranding its story; a proposal's row and ledger event disagreeing |
| 3 | ACCEPT_WITH_RESERVATIONS | The deadlock tests could be released by another test's lock wait (since closed: they now look only at their own connections) |

## Slice 3a-ii, design

| Round | Astra's disposition | What it found |
|---|---|---|
| 7 | CHALLENGE, 10 blocking across four steps | Closure recorded on stories deadlocks against release and misses a feature with no stories; pause and cancel not told apart; no boundary between starting work and recording it; a refused delivery lost; an interrupted batch stranded. Five more for the later steps. |
| 8 | CHALLENGE, 5 blocking | A grant outliving its consumer; an obsolete build command becoming valid again; resume missing work that has no command; an older retry displacing newer checks; "nothing is delivered" undefined |
| 9 | CHALLENGE, 4 blocking | A review granted before a pause stranded after resume; closure rules dropped by an amendment that "replaced" sections; a late green answering a replacement assignment; re-announcement undefined after Redis is emptied |
| 10 | CHALLENGE, 3 blocking | Three restart cases with work in flight, for which Astra supplied the wording |
| 11 | ACCEPT | Nothing new |

The agreed design for step 1 (pause, resume, cancel) is `round-10-claude-3a-ii-step-1-design-v3.md`, which stands alone. Carried to the design rounds of the later steps: S3-4.6 and S3-4.7 (step 2, questions and decisions), S3-4.8 and S3-4.9 (step 3, corrections), S3-4.10 (step 4, feature completion).

What this design round showed: the first two drafts inferred what work was owed from queued messages and behaviour state. Recording it (an assignment with a grant that has an identity) answered five findings at once.

## Slice 3a-ii, step 1 as built (pause, resume, cancel)

Reviewed under decision D-8: at most three rounds, then whatever is reachable is fixed without a further round.

| Round | Astra's disposition | What it found |
|---|---|---|
| 12 | CHALLENGE, 7 blocking of 8 | A successor able to launch a native check; a restarted reviewer's capability approving on a dead worker's checkout; a closure receipt replayed across projects; a worker able to claim an integration and strand it; a pause stopping the whole start-up pass; a successor cut short left with nothing owed; "latest batch" decided by transaction start, not registration; held checks reported as waiting after a cancel |
| 13 | CHALLENGE, 4 blocking of 5 | A worker whose grant was taken back submitting on its successor's checkout; a successor cancelled during settlement; a successor overtaken by a new required check left with nothing owed; a paused feature's integration reaching a slow repository; interrupted checks reported as waiting after a cancel |
| 14 | CHALLENGE, 2 blocking of 3 | A check policy change during a resume leaving the work to wait for another call; one project's silent repository holding up the pass for the others; the reason for a re-run not kept |

Every finding in all three rounds was reachable through a real entry point, and every one is fixed, each with a test that fails without the fix. The fixes for round 14 are in `round-15-claude-3a-ii-step-1-closing.md` and were **not reviewed by Astra**: that is what D-8 means, and it is the standing caveat on this step.

Seven departures from the agreed design are listed in `round-12-claude-3a-ii-step-1-submission.md`; Astra accepted all of them once rounds 12 and 13 were fixed.

Reservations carried forward:

- **A pass runs on resume and on start, not on a timer.** Work that becomes owed outside a pass (a successor whose checks keep being overtaken, past three attempts in one pass; a repository that could not be read) waits for the next resume or start. The periodic loop belongs to 3c.
- **Only the pass's own calls to a repository are bounded** (publication, recovery, the successor's trunk read). A caller's own `integrate`, `run_red` and `run_green` still wait for Git as long as Git takes.
- **A resume runs the regression checks before it returns**, because the control plane is the only integrator until 3b.
- **"Restart" in the tests is new service objects over the same database**, with the old task cancelled. No test kills an operating-system process. The publisher cases stand a recording object where Redis would be.
- The round 14 fixes have had no second pair of eyes.

What this step showed: three rounds, sixteen findings, none of them speculative. Most came from one pattern, an obligation recorded as done at the moment its work started rather than when it finished, so anything that interrupted the work left nothing owed. The design said "an assignment is done when its effect is on record"; taking the successor batch's existence as that effect was my reading, and it was too early.

## Slice 3a-ii, step 2, design (questions, answers and changed decisions)

| Round | Astra's disposition | What it found |
|---|---|---|
| 16 | CHALLENGE, 7 blocking of 8 | A submission stamped with the basis current when it arrives, not the one its author read; an old result arriving after reconfirmation; a story awaiting approval with no way back; amendment routes that do not exist; assignments that need a behaviour; blockers not composing; no rule for one answer per question |
| 17 | CHALLENGE, 6 blocking of 7 | Red run on a stale contract; an amendment needing no grant; an old checkout indistinguishable from a new one; an outstanding push at reconfirmation; nothing waking held work when the last blocker clears; context and basis read apart |
| 18 | CHALLENGE, 4 blocking of 5 | A roadmap compared with the wrong scope's basis; a refused reconfirmation left granted to one worker; an old seal surviving an answer; reopening a red removing the reconfirmation it needs |

The build contract is `round-19-claude-3a-ii-step-2-design-v4.md`: design v3 with round 18's five findings folded in. **Astra has not reviewed v4.** It kept the core of the design (one decision number per story, scope looked up and never copied) through all three rounds, and said the last amendments could be folded in without another design round.

What step 2 will not do, by design: change anything already in review, integrated or approved, or change a story's text or a roadmap's content. Those are corrections, step 3. Until step 3 exists, a scope stopped for such a reason stays stopped and says why.

Conditions carried to 3b: a principal on every worker-callable operation; durable task and invocation identity before any provider is called; a worker receives the story and list revisions with what it reviews and hands them back unchanged.

All replies are from `gpt-6.2-astra`. Agreement between two models is not evidence of correctness.
