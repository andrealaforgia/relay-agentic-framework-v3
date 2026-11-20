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

Conditions carried to 3b: a principal on every worker-callable operation; durable task and invocation identity before any provider is called; a worker receives the story and list revisions with what it reviews and hands them back unchanged.

All replies are from `gpt-6.2-astra`. Agreement between two models is not evidence of correctness.
