# Slice 3a-ii, step 2 as built: fixes after round 2 (round 3 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. Code at commit `5938363` on `main`. `make check` passes: 613 tests. This is the third and last round under D-8: what you find reachable now I fix without a further round, and the rest becomes named reservations in the README.

All five findings accepted and fixed. Each test was written and seen to fail first.

| Finding | Fix | Test (`tests/integration/test_decision_review.py`) |
|---|---|---|
| 3AII-S2-2.1 a spent grant acknowledging something else | What a grant completed is recorded under it, exactly as submitted (`reconfirmations`, one row per grant, the grant unique): the subject, the basis, and the revision, or the contract digest with the evidence retained, or the old and new digests of an amendment. A repeat is acknowledged only if it is identical; anything else under that grant is refused. An amendment is recorded there too, so its exact replay is recognised whatever the contract has become since. | `test_a_grant_that_has_been_used_acknowledges_only_exactly_what_it_did`: a changed revision; a switch from amendment to reconfirmation; a different amendment; and the first amendment replayed exactly after a later one |
| 3AII-S2-2.2 a refused publication blocking a stale cycle | The publication path's three terminal failures go through `_stop_publication`, which blocks only if the story and contract are current under the behaviour's lock; otherwise it records the failure as deferred and leaves the cycle pending. | `test_a_refused_publication_does_not_block_a_cycle_a_decision_has_made_stale`: the decision lands while the push is on its way, the remote refuses, the cycle stays open and pending, and publication succeeds after reconfirmation |
| 3AII-S2-2.3 an assumption recorded on a story approved a moment before | `assume` takes the project lock before it looks at the story. | `test_an_assumption_about_a_story_approved_a_moment_ago_is_refused` holds the approval with the project row |
| 3AII-S2-2.4 not every reason | `not_current` returns the story's reason and the contract's together. `prepare_review`, `seal` and `assess` use `require_startable`, as the other starts do. | `test_a_refusal_names_the_question_the_story_and_the_contract_together`: a question open with both the story and the contract stale, through `reopen_red`, `prepare_review` and `seal` |
| 3AII-S2-2.5 the rejection naming the later contract | The domain judges and words a green against the contract it was launched to observe. | asserted in `test_a_green_is_recorded_under_the_contract_it_was_observed_against` |

What I know about these fixes:

- 2.2 covers the publication path. `prepare_review` still blocks a cycle when the candidate ref no longer names the commit, as before; it cannot reach that point on a stale cycle, because it now refuses first.
- In 2.4, `seal` and `assess` name the closure and the story, then each listed contract in turn in a second refusal. A list with two stale contracts is told about the first.

Please review `5938363`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 2 as built), saying for each finding whether a real entry point can reach it.
