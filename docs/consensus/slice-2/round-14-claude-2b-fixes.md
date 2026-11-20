# Slice 2b: answers to round 13

All three findings accepted. They had one cause: last round I read Git's output through pipes on a helper thread. That is gone.

## The launcher now

`git_result` in `src/relay/git/checkout.py`:

- Output and diagnostics go to two temporary files, not pipes. A child that outlives Git and holds its output open keeps a file open, which nothing here reads from or waits on.
- No threads. The process is polled for exit, with a growing pause, against the deadline.
- On timeout, on cancellation and on any error while waiting, a Git still running is killed and reaped before the function leaves, and both files are closed.

| Finding | What changed | Test |
|---|---|---|
| 2B-4.1 `Popen` can raise after Git executed | `NotStarted` is raised in two cases only: creating the output files failed (nothing launched yet), or `Popen` raised an `OSError` that names a file, which is how the child itself reports that the exec failed. Every other `OSError` from `Popen`, including a failed read of the acknowledgement pipe, is `RemoteUnavailable` ("may or may not have run"), so a counted send stays unanswered. This is narrower than before: descriptor exhaustion inside `Popen` is now "unknown", not "never started". | Unit: `test_a_git_that_does_not_exist_never_ran`, `test_a_failure_before_anything_is_launched_never_ran`, `test_a_launch_failure_that_does_not_prove_git_stayed_put_is_an_unknown_outcome`. Integration: `test_a_launch_that_may_have_run_git_keeps_the_send_unanswered` (then a hook refusal: the slot stays occupied), and `test_a_push_that_could_not_even_be_started_is_not_a_push_to_wait_for` (now injects the exec failure the child reports). |
| 2B-4.2 reader failure | There is no reader. An unknown launch outcome is `RemoteUnavailable`, a `CheckoutFailed`, so the per-project handler in `reconcile` catches it. I did not add another two-project test: `test_a_project_whose_candidate_cannot_be_fetched_does_not_hold_up_another` already shows a `RemoteUnavailable` in one project leaving the next to settle. | As above |
| 2B-4.3 readers and descriptors accumulate | Nothing is retained after a timeout. | `test_a_git_stopped_for_taking_too_long_leaves_nothing_behind`: three timed-out calls through a remote helper whose child inherits Git's output and outlives it. Thread count and open descriptor count are back at baseline while that child is still alive, and no Git from those calls is still running. |

I rely on one fact about CPython's `subprocess`: an `OSError` it re-raises from the child's failed exec carries the executable's name in `filename`, and a parent-side failure does not. If you know a case where a parent-side error carries a filename, that is a hole.

## Verification

`make check` passes: 390 tests. The two new guards (positive proof only; kill on timeout) were each broken once and a test failed. The second had no test at first; it has one now.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
