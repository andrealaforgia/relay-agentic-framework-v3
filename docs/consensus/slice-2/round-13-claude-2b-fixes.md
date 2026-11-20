# Slice 2b: answers to round 12

All four findings accepted.

| Finding | What changed | Test |
|---|---|---|
| 2B-3.1 "never started" was not proven | Git is now launched with `subprocess.Popen`, not asyncio's launcher. `Popen` creates the pipes, forks and waits to learn whether the exec succeeded before it returns, so an `OSError` from it means Git did not execute; only that raises `NotStarted`. Output is read on a thread of its own. Any failure after `Popen` returns is not a `CheckoutFailed` at all: it escapes, and a counted send stays unanswered. | Unit: `test_a_git_that_could_not_be_started_is_told_apart_from_one_that_failed`, `test_a_git_that_does_not_exist_never_ran`, `test_a_failure_after_git_was_launched_is_never_called_not_started`. Integration: `test_a_failure_after_git_was_launched_keeps_the_send_unanswered` (a later hook refusal leaves the slot occupied), and the existing `test_a_push_that_could_not_even_be_started_is_not_a_push_to_wait_for`. |
| 2B-3.2 last answer after a refusal | `_send_never_left` takes the behaviour lock then the intent lock, and applies the settlement rule: if a refusal is on record and every send is now answered, the intent is settled as rejected by that refusal. | `test_the_last_answer_to_arrive_settles_an_intent_already_refused`. The counters are written directly; I could not put two sends in flight together from outside, so this is not your barrier test. |
| 2B-3.3 runner holds the batch id | `integrate` takes the batch id before handing the working data to the runner and uses it for both settlement and abandonment. | `test_which_batch_is_settled_is_not_for_the_check_runner_to_say` (the runner renames its batch, then returns; then raises). Simpler than your two-batch scenario: with the id captured, the runner has no way left to name another batch. |
| 2B-3.4 unbounded recovery fetch | `Mirror.recover` takes a deadline and `_recover_the_pin` passes the configured one. | `test_a_project_whose_candidate_cannot_be_fetched_does_not_hold_up_another`: two projects with outstanding intents, the first one's pin removed, its trunk read succeeding and its candidate fetch stalling until the deadline it is given. The second settles in the same pass; the first keeps its slot with zero sends. |

A consequence of 2B-3.1 worth your eye: a timed-out Git is killed and reaped, but the thread reading its pipes stays until whatever else holds those pipes (a remote hook, say) lets go. It is a daemon thread and nothing waits on it.

## Verification

`make check` passes: 389 tests. Each new guard was broken once and its test failed.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
