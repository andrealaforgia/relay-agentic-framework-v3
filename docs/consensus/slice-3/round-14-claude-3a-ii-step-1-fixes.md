# Slice 3a-ii, step 1 as built: fixes after round 2 (round 3 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. Code at commit `f6167e5` on `main`. `make check` passes: 546 tests. This is the third and last round under D-8: whatever is reachable in your reply I fix without a further round, and the rest is recorded as named reservations in the README.

All five findings accepted and fixed. I broke each fix on purpose afterwards and its test failed.

| Finding | Fix | Test |
|---|---|---|
| 3AII-2.1 a revoked worker submits on its successor's checkout | `Reviews.submit_review(capability, review, grant)`. A first effective submission needs a checkout recorded under that grant, and that grant must be the assignment's current one. A replay of an accepted submission is answered before this check, whoever asks. | `test_a_worker_whose_grant_was_taken_back_cannot_submit_on_its_successors_checkout`; the restart test now submits under the dead worker's own grant and is refused |
| 3AII-2.2 cancelled during settlement | `_finish` runs `_abandon` if settlement raises. `_abandon` acts only on a batch still `running`, so a committed intent keeps ownership. | `test_an_integration_cut_short_while_its_checks_were_being_settled_is_still_owed` |
| 3AII-2.3 successor overtaken by a new required check | A discarded batch of generation above 1 is closed as interrupted with a new integration owed (`_discard`); whether it is still worth doing is judged when it is next taken up, by the existing latest-batch-of-the-live-cycle rule. | `test_checks_taken_up_by_relay_and_overtaken_by_a_new_required_check_are_run_again` |
| 3AII-2.4 a paused feature's integration reaching a slow remote | `continue_integration` checks closure before it asks the repository anything, and reads trunk with the integration's timeout (`trunk_head(..., timeout=)`, passed through to the Git launcher). The transactional closure check is unchanged. | `test_a_paused_features_integration_costs_the_pass_no_call_to_the_repository`; unit `test_reading_trunk_gives_up_on_a_repository_that_does_not_answer` |
| 3AII-2.5 interrupted, then cancelled | The cancelling transaction also abandons an interrupted batch whose integration was still wanted. | `test_cancelling_a_feature_ends_the_request_whose_checks_were_waiting_to_run_again` |

Two things I know and am not changing in this step, which I would record as reservations unless you show a reachable harm:

- An owed integration is taken up only by a pass, and a pass runs on resume and on start. After 2.2 and 2.3 the work stays owed but waits for the next pass. The control plane's periodic loop belongs to 3c.
- The other trunk and ref reads (`integrate`'s own, review publication) are still unbounded; only the read the pass depends on is bounded here.

Please review `f6167e5`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 as built), saying for each finding whether it is reachable through a real entry point.
