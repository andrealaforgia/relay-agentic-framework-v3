# Slice 3a-ii, step 3, part (i) as built: fixes after round 1 (round 2 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for Astra. Code at commit `efae0d5` on `main`. The last full `make check` ran 662 tests: 661 passed and one failed, an older test comparing the whole change list that did not yet expect the new `moved` entry. I corrected that test and added one more (below); both pass on their own, and no source has changed since that run. Second of at most three rounds (D-8).

All seven findings accepted and fixed, and the wording you challenged in departure 3. Tests are in `tests/integration/test_corrections_review.py`.

| Finding | Fix | Test |
|---|---|---|
| 3AII-S3-I-1.1 a proposal against a reworded criterion | `Behaviours.propose` names the story revision its author read, compared under the story lock with the basis. | `test_a_behaviour_is_proposed_against_the_story_revision_its_author_read`: same criterion name, same basis, both revisions; the stale call leaves no behaviour and no event |
| 3AII-S3-I-1.2 deadlock with `reconfirm_story` | `publish_roadmap` and the decision of a roadmap gate take the gate lock exclusively, as design v4 said. I withdraw departure 2. | `test_reconfirming_a_story_and_a_roadmap_that_edits_it_do_not_deadlock`, for approval and for publication, with a valid grant, the reconfirmation held between the story and the project rows |
| 3AII-S3-I-1.3 two features, one story name | Approval checks again that every new name is unused in the project; a collision is a durable stale receipt that names it. `_settle_roadmap` now returns why it did not apply. | `test_two_features_proposing_a_story_of_the_same_name_do_not_both_get_it`, with the second request replayed |
| 3AII-S3-I-1.4 a conflict seen as a story is rejected | `_block` changes nothing when the scope has ended (story rejected or withdrawn, feature cancelled), for every caller including the publisher. | `test_a_conflict_seen_as_a_story_is_rejected_blocks_nothing` |
| 3AII-S3-I-1.5 an interrupted batch reported as held | Rejection abandons the story's interrupted batches whose integration was still wanted, before it retires the assignments that identify them, as cancellation does. | `test_rejecting_a_story_ends_the_request_whose_checks_were_waiting_to_run_again`, with a start afterwards |
| 3AII-S3-I-1.6 a reorder showing nothing | The manifest's changes list what moved, with the place it had and the place it gets, and a kept or reworded correction shows what it corrects. | `test_a_reorder_tells_the_person_what_moved_and_what_each_correction_corrects` |
| 3AII-S3-I-1.7 withdrawn turned into cancelled | Cancellation leaves a withdrawn story as it is. | `test_a_story_that_was_dropped_stays_withdrawn_when_its_feature_is_cancelled` |
| Departure 3, the wording | Where a result settles after the scope ended, the record says what ended: "S1 was rejected", not "the feature was cancelled". | `test_checks_that_finish_after_their_story_was_rejected_say_so` |

What I should say about these:

- I wrote these tests before the fixes but ran them only afterwards, so I did not see each one fail. So I broke each fix on purpose afterwards: eight of nine were caught at once. The ninth, the exclusive lock on publication, was only exercised through approval; the deadlock test now runs for publication too and catches it.
- 1.6: the test covers moves; it does not build a reorder that contains an existing correction, so the correction target on a kept story is untested.
- The wording fix is applied where a check batch settles. Other refusals that reach the `cancelled` arm for a rejected story still say "the feature is cancelled".

Please review `efae0d5`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 part (i) as built), saying for each finding whether a real entry point can reach it. One round remains after this.
