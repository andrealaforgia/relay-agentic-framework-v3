# Slice 2b: answers to round 11

All five findings accepted.

| Finding | What changed | Test |
|---|---|---|
| 2B-2.1 intent built from a mutable dictionary | `_settle_batch` now takes only the batch id. It reads the batch row under lock and uses that row for everything: the completeness check, the staleness check, the remediation and the intent. Nothing the check runner was holding reaches the intent. | `test_what_is_authorised_is_the_batch_on_record_not_what_the_runner_was_holding`. It fails on the previous `integration.py` (the tampered values reached the intent) and passes now. |
| 2B-2.2 spawn failure leaves a phantom send | `git_result` raises `NotStarted` when the subprocess cannot be created. In `_drive`, a counted send that was never started is marked answered (`integration.push_not_started`), so a later refusal can settle. A crash during the handoff keeps the conservative treatment: the send stays unanswered. | `test_a_push_that_could_not_even_be_started_is_not_a_push_to_wait_for`, `test_a_git_that_could_not_be_started_is_told_apart_from_one_that_failed` |
| 2B-2.3 mirror lost after authorisation | Before anything is counted, `_drive` makes sure the candidate is pinned, recovering it from the cycle's published candidate ref if the mirror has lost it. | `test_a_mirror_lost_after_authorisation_is_rebuilt_from_the_published_candidate` |
| 2B-2.4 unbounded read | `Mirror.read_ref` takes a deadline and reports expiry as `RemoteUnavailable`, which `reconcile` already treats per intent as "nothing learned" and which, after a refusal, is treated as an unverifiable reading. The same deadline as the push is used. | `test_a_repository_that_never_answers_a_read_is_given_up_on_in_time` (a socket that accepts and never speaks). I did not build your two-project test: the per-intent handling is covered by `test_a_repository_that_cannot_be_reached_leaves_the_intent_for_the_next_pass`, and the new test covers the deadline. Say if you want the combined one. |
| 2B-2.5 obsolete answer after convergence | `_unanswered` locks the intent and returns its settled receipt if it is no longer outstanding, without appending an event. | `test_a_send_whose_error_arrives_after_the_integration_was_settled_says_integrated` (calls the private step directly; no barrier) |

One thing I added and then removed: a comparison of the cycle's candidate with the batch's candidate at authorisation. No test could distinguish it, because the batch is bound to the cycle by id and a cycle's candidate never changes.

## Verification

`make check` passes: 382 tests. Each new guard was broken once and its test failed, except 2B-2.1, which I verified the other way round as stated above.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
