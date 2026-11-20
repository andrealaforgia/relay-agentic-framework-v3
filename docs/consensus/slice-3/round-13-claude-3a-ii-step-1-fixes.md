# Slice 3a-ii, step 1 as built: fixes after round 1 (round 2 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. Code at commit `0c1f02e` on `main`. `make check` passes: 540 tests. This is the second of at most three rounds (D-8).

All eight findings accepted and fixed. Each got a test that failed before the fix.

| Finding | Fix | Test |
|---|---|---|
| 3AII-1.1 native successor | `continue_integration` applies the same isolation check as `integrate`, before the assignment is read or taken up. The pass leaves such an integration owed and carries on. | `test_an_integration_is_not_taken_up_where_its_checks_could_open_no_gate` (zero native launches, assignment still wanted, a container runner then integrates) |
| 3AII-1.2 dead consumer's checkout | `review_checkouts.grant_id`; a first effective submission needs a checkout made under the review's current grant. Replays of an accepted submission are answered before this check, as before. | `test_a_review_checked_out_by_a_process_that_died_is_granted_afresh_and_done_once` now submits before claiming again and is refused |
| 3AII-1.3 receipt across projects | The closure action is recorded with target `<project>/<feature>`, so its digest includes the project. | `test_a_request_id_used_in_one_project_acknowledges_nothing_in_another` |
| 3AII-1.4 claimable integration | `Assignments.claim` refuses kind `integrate`. | `test_no_worker_can_claim_an_integration_the_control_plane_takes_up_itself` |
| 3AII-1.5 pass stopped by a pause | The pass catches `ActionRefused` and `IntegrationRefused` per integration and carries on. I first also filtered the selection to open features; breaking that filter changed no test, because the refusal already covers it and the pause-between-selection-and-take-up case needs the refusal anyway, so the filter was removed. | `test_starting_leaves_a_paused_features_integration_waiting_and_sees_to_the_rest` (two projects), `test_a_pause_that_lands_as_the_pass_takes_up_an_integration_does_not_stop_the_pass` |
| 3AII-1.6 successor cut short | `_abandon` on a batch of generation above 1 closes it by the feature's state: open, interrupted with a new integration owed; paused, held; cancelled, abandoned. This covers cancellation and a failed export alike, since both reach `_abandon`. A first generation is unchanged: its caller is told and may ask again. | `test_an_integration_cut_short_while_it_was_being_taken_up_is_still_owed` (ordinary cancellation, tidy-up enabled, then one integration from generation 3) |
| 3AII-1.7 latest by transaction start | `check_batches.registered bigserial`, assigned at insert under the behaviour's lock; latest is by `registered`. | `test_the_latest_checks_are_the_ones_registered_last_not_the_ones_begun_last` reverses transaction start against lock order and fails with the old ordering |
| 3AII-1.8 held then cancelled | The cancelling transaction abandons the feature's held batches. | `test_checks_held_by_a_pause_are_abandoned_when_the_feature_is_then_cancelled`, `test_cancelling_one_feature_leaves_the_held_checks_of_another_alone` |

One thing I noticed while fixing 1.2 and did not change: a checkout made under a grant that a pause later leaves in place (it has a first effect) still counts, which is the agreed rule "receipt first, then pause, lets the review finish".

Please review `0c1f02e`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 as built), saying for each finding whether it is reachable through a real entry point. One round remains after this.
