# Slice 3a-ii, step 2 as built: closing the step after round 3

Provenance: written by Claude (Fable 5.2) on 2026-11-20. Not sent to Astra: under D-8 the third round was the last, and these fixes close the step without a further review. That they are unreviewed is recorded as a reservation in the README.

All three findings of round 22 accepted and fixed. Each test was written and seen to fail first, in `tests/integration/test_decision_review.py`.

| Finding | Fix | Test |
|---|---|---|
| 3AII-S2-3.1 a late refusal blocking a published cycle | A publisher's terminal failure blocks the cycle only if, under the behaviour's and the cycle's locks, the cycle is still waiting to be published. Otherwise what was seen is recorded as deferred and nothing changes. | `test_a_refusal_seen_by_one_publisher_does_not_undo_anothers_publication` |
| 3AII-S2-3.2 a conflict seen while a decision lands | `_block` leaves a stale cycle alone for every caller, not only the publisher: what a caller saw before taking the locks has authority only if the cycle is still current. The checkout is refused either way. A real conflict is seen again, and blocks, when work resumes after reconfirmation. My round 22 statement that preparation could not reach `_block` on a stale cycle was wrong, as Astra showed. | `test_a_conflict_seen_while_a_decision_lands_blocks_nothing_until_it_is_seen_again`, with the ref right afterwards and with it still wrong |
| 3AII-S2-3.3 a seal told about one thing at a time | `seal` and `assess` collect the closure, the story and every listed contract that is not current, and refuse once. | `test_a_seal_is_refused_once_with_the_question_the_story_and_every_stale_contract` |

Limits, stated plainly:

- Both races were tested by making the interleaving happen inside one process, with a stand-in for the Git call. No test runs two publisher processes.
- In 3.2 a stale cycle is never blocked by an observation. If the conflict is real and nobody ever reconfirms, the cycle stays open and stale; it cannot be reviewed or integrated in that state, so nothing is at risk, but nothing says "blocked" either.
