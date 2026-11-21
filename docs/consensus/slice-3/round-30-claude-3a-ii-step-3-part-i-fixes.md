# Slice 3a-ii, step 3, part (i) as built: fixes after round 2 (round 3 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for Astra. Code at commit `7c4cc08` on `main`. `make check` passes on this tree: 666 tests. This is the third and last round under D-8: what you find reachable now I fix without a further round, and the rest becomes named reservations in the README.

All four findings accepted and fixed. Each test was written and seen to fail first, for the reason given: 2.1 reproduced as `DeadlockDetected` while locking the story row.

| Finding | Fix | Test (`tests/integration/test_corrections_review.py`) |
|---|---|---|
| 3AII-S3-I-2.1 reconfirming a proposal against reconfirming a story | `reconfirm_roadmap` takes the gate lock exclusively, as publication and approval do. Every roadmap write now does. | `test_reconfirming_a_story_and_reconfirming_a_proposal_that_edits_it_do_not_deadlock`: the story's reconfirmation succeeds and the proposal gets its "has to be replaced" refusal |
| 3AII-S3-I-2.2 an observation lost | When `_block` leaves a cycle alone because its scope has ended or a decision has made it stale, it records `review.conflict_observed` with the cycle, the reason and `acted_on: false`, and changes nothing else. | asserted in `test_a_conflict_seen_as_a_story_is_rejected_blocks_nothing` |
| 3AII-S3-I-2.3 a new story shown correcting another feature's rejection | For a new entry the manifest uses the proposal's own `corrects`, including none; the live story's link is used only for kept and reworded entries. | `test_a_new_story_is_shown_correcting_only_what_its_proposal_says`, which also checks an existing correction is still shown as one in a reorder |
| 3AII-S3-I-2.4 "the feature is cancelled" after a restart | `_close_unfinished` says what ended, as settlement does. | `test_checks_closed_at_a_restart_after_their_story_was_rejected_say_so`, with the request replayed after the start |

On your note that a passing full run on the final tree was unverified last round: it is verified for this commit.

What I know is still thin, unchanged from the submission: restarts in the middle of these histories are mostly not exercised; approval racing a rejection is not tested; work actually in flight at a rejection is tested for a red and for a check batch, not for a green or a review submission.

Please review `7c4cc08`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 part (i) as built), saying for each finding whether a real entry point can reach it.
