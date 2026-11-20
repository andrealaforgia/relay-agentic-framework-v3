# Slice 2b: answers to round 10

All eight findings accepted. None is disputed.

| Finding | What changed | Test |
|---|---|---|
| 2B-1.1 partial batch | Before anything is recorded, settlement compares the runs returned with the members persisted on the batch (by digest, as a multiset) and each run's revision with the batch's candidate. Any difference discards the batch. An empty regression set still authorises. | `test_runs_that_are_not_every_check_on_the_candidate_authorise_nothing` (one check missing; run on another commit), `test_a_project_with_no_checks_at_all_still_integrates` |
| 2B-1.2 unfenced release | `Mirror.read_ref` makes one fetch and answers both "where is the ref" and "does that same commit strictly descend from the expected head". `read_trunk` now has four readings: converged, undecided, conflict (only a head beyond the expected one), unverifiable (older or unrelated: the slot is kept and nothing is sent). | `test_a_reading_of_trunk_behind_the_expected_head_settles_nothing`, unit tests in `test_integration_rules.py`, `test_a_ref_is_read_once_for_both_where_it_is_and_whether_it_moved_on` |
| 2B-1.3 merge that discards the change | Convergence is the exact candidate only. Departure 4 is withdrawn. A later head, whether or not it contains the candidate, is a conflict. | `test_a_later_head_that_contains_the_candidate_is_not_taken_as_its_integration` (built on top; merged with the change discarded) |
| 2B-1.4 two repositories | `open_project` stores a local path made absolute (`canonical_repository`); URLs and scp-style addresses are kept as given. The intent records the repository and trunk it was authorised for, and every later read and push uses those. | `test_a_repository_enrolled_by_a_relative_path_is_the_one_that_moves`, `tests/unit/test_repository_address.py` |
| 2B-1.5 duplicate obligation | The regression set is deduplicated by digest before it is frozen. | `test_an_obligation_that_arrives_twice_is_one_check_run_once` |
| 2B-1.6 phantom send | `Mirror.advance` establishes locally that the commit is pinned, that the expected head is available and that the update is a fast-forward, and only then calls back to count the send, immediately before the push. A local failure before that is not a send. A missing expected object is "cannot say", not "not a fast-forward". A definitive local "not a fast-forward" settles the intent only if no send is unanswered. | `test_a_local_failure_before_anything_is_sent_is_not_a_push_to_wait_for`, `test_the_caller_is_not_asked_to_send_what_cannot_be_checked_locally`, `test_nothing_is_sent_when_the_caller_declines_at_the_last_moment` |
| 2B-1.7 cycle reused after a conflict | `_block`, when it finds the cycle owned by an authorised integration, records the conflict on the cycle. When that intent is rejected the cycle becomes `blocked` with that reason, and `retry_integration` refuses it. | `test_a_conflict_seen_under_an_intent_that_is_then_refused_stops_the_cycle_being_reused` |
| 2B-1.8 third send | The refusal is looked at again under the lock that counts the send. | The two-barrier test now drives the intent once more and asserts the count stays at two |

## Your two answers, applied

- **Trunk moved on.** Any reading that shows trunk strictly beyond the expected head and not at the candidate settles the intent as `conflicted` and reopens the red directly, with no person in between. A remote refusal triggers one fresh reading first: a conflict reading is handled this way; trunk still at the expected head keeps the refusal path and the human retry gate; an unverifiable or failed reading is treated as a plain refusal of that send, which settles only if no send is unanswered. `retry_integration` now serves rejected intents only.
- **One unanswered send, then a refusal.** The slot is kept. The ledger event now carries the intent, the expected head, the candidate, the refusal and what is being waited for.

The runtime-change receipt is now `runtime_changed`, and a repeated request gets the same.

## Verification

`make check` passes: 376 tests, lint and strict typing, against real PostgreSQL, Redis, Git and Docker.

I broke 20 of the new guards one at a time. 17 were caught at once. Two were not, and each now has a test: a local failure before sending must leave no "push unanswered" event, and a local "not a fast-forward" must settle without counting a send (`test_an_update_found_locally_to_be_no_fast_forward_is_refused_without_a_send`). The third survivor is an equivalent change: letting `retry_integration` accept a conflicted intent makes no difference, because a conflict always supersedes its cycle and the retry requires a satisfied one.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
