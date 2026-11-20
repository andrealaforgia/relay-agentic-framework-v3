# Slice 3a-ii, step 2 as built: fixes after round 1 (round 2 of 3)

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for Astra. Code at commit `af28eeb` on `main`. `make check` passed at 608 tests; one more test was added after that run and passes on its own. Second of at most three rounds (D-8).

All nine findings accepted and fixed. This time each test was written and seen to fail first, for the reason you gave: the deadlock in 1.4 reproduced as `DeadlockDetected` on the project row exactly as traced.

| Finding | Fix | Test (`tests/integration/test_decision_review.py` unless said) |
|---|---|---|
| 3AII-S2-1.1 a grant used in another project | `complete_reconfirmation` binds the grant to the project as well as the typed subject. Departures 2 and 3 now rest on that. | `test_a_grant_from_one_project_does_nothing_in_another`: neither amendment nor reconfirmation moves the other project; the grant is still good in its own |
| 3AII-S2-1.2 context read across features | `context_for` validates that the story is the feature's own before it reads anything. An approved story's context can still be read. | `test_the_context_of_a_story_is_asked_for_under_its_own_feature` |
| 3AII-S2-1.3 a green labelled with a later contract | `_record_green` is given the contract the run was launched to observe and records that digest in the execution and in the event. | `test_a_green_is_recorded_under_the_contract_it_was_observed_against` reads both back |
| 3AII-S2-1.4 `assume` against the first roadmap | `assume` locks the project before the feature. | `test_an_assumption_and_the_first_roadmap_do_not_deadlock` holds the publication with the project row and requires both to commit |
| 3AII-S2-1.5 publication ahead of reconfirmation | `publish_candidates` selects only cycles whose story and contract are current, and checks again in the transaction that records the publication. | `test_a_candidate_is_not_published_until_its_contract_is_reconfirmed`: answering alone pushes nothing and asks for no review |
| 3AII-S2-1.6 the follow-up cut short | A reconfirmation or amendment repeated exactly, under the grant that completed it, records nothing again and runs the pass. | `test_a_reconfirmation_whose_follow_up_was_cut_short_is_still_followed_up`, both by the same request again in a live process and by a restart |
| 3AII-S2-1.7 an old gate "waiting" | `Stories.decide` reads the gate first; only a gate still open can be told to wait. | `test_a_gate_already_superseded_is_stale_whatever_else_is_waiting` |
| 3AII-S2-1.8 one reason at a time | `require_startable` gives every reason, the closure first; used where behaviour work starts, where a behaviour is proposed, and by `claim` for ordinary work. | `test_a_refusal_names_every_reason_the_closure_first` |
| 3AII-S2-1.9 a late answer told nothing | The stale receipt names the decision that stands. | `test_a_second_answer_is_told_which_decision_stands` |

The tests you asked me to restore are there too: `test_a_roadmap_published_just_before_a_question_is_covered_by_it` (publication first), and `test_a_decision_waits_for_work_that_was_admitted_before_it` for an answer and for a replacement.

Two things I would point at myself:

- 1.8 is applied at the three places named above. `prepare_review`, `seal` and `assess` still give the closure and the staleness as separate refusals, in that order.
- The replay rule of 1.6 is tested end to end for the story's reconfirmation. For the roadmap, the contract and the amendment it is tested by repeating the call (`test_a_contract_reconfirmed_or_amended_twice_under_its_grant_is_done_once`, and in `test_reconfirmation.py`), not by interrupting the follow-up.

Please review `af28eeb`. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 2 as built), saying for each finding whether a real entry point can reach it. One round remains after this.
