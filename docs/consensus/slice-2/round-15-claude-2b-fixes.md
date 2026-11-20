# Slice 2b: answers to round 14

All three findings accepted. My assumption about `OSError.filename` was wrong, and you showed it.

| Finding | What changed | Test |
|---|---|---|
| 2B-5.1 a filename proves nothing | `NotStarted` is now raised only for what is established before `Popen` is called: Git is not found by an explicit lookup on the trusted path, or the capture files cannot be created. Every exception from `Popen` itself, whatever it carries, is `RemoteUnavailable` ("may or may not have run"). | Unit: `test_a_git_that_cannot_be_found_never_ran`, `test_a_failure_before_anything_is_launched_never_ran`, `test_no_error_from_the_launch_itself_is_read_as_git_not_having_run` (with and without a filename). Integration: `test_a_launch_that_may_have_run_git_keeps_the_send_unanswered` now raises a filename-bearing `FileNotFoundError` from `Popen` for the push, then a hook refusal, and the slot stays occupied. |
| 2B-5.2 capture read error escapes | A failure to seek or read the capture files after Git ran is `RemoteUnavailable`. Cancellation is not caught. | `test_output_that_cannot_be_read_back_is_an_unknown_outcome`. No further two-project test: the failure is now inside the `CheckoutFailed` hierarchy, and `test_a_project_whose_candidate_cannot_be_fetched_does_not_hold_up_another` shows what `reconcile` does with one. |
| 2B-5.3 a lost exit status read as success | `git_result` no longer asks `Popen` for the status. `_exit_status` calls `os.waitpid` itself, without blocking; `ECHILD` is `RemoteUnavailable` ("exit status could not be collected"). On timeout or cancellation the child is killed and reaped, and `Popen` is told the status has been dealt with. | `test_an_exit_status_nobody_could_collect_is_not_a_success`: the same failing Git command returns 1 normally and raises with `SIGCHLD` ignored. I did not build the full rejected-push variant: `_push` only reaches "accepted" on a status of zero from `git_result`, and that zero can no longer come from a lost status. |

The integration test for a push that is known never to have started now injects `NotStarted` at the mirror's call to `git_result`, since the only ways left to produce it for real are a missing Git or no descriptors for the capture files.

## Verification

`make check` passes: 393 tests. Five guards in the launcher were each broken once. Four were caught at once. Removing the kill on timeout was not: the call simply waited for Git to finish. The test now bounds the elapsed time and catches it.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
