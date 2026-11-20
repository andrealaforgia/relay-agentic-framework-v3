# Slice 2b: answers to round 15

Both findings accepted.

| Finding | What changed | Test |
|---|---|---|
| 2B-6.1 a signal to a PID Relay no longer owns | Two changes. Before anything is launched, `git_result` refuses (`NotStarted`) if this process ignores `SIGCHLD`, since the system would then reap Git by itself: Relay must be the one that collects it. And cleanup (`_stop`) first asks, without blocking, whether the child is still an unreaped, running child of this process. If it has been reaped by anyone, or that very call reaps it, no signal is sent. Only a child that is still ours and still running is killed, and an unreaped child keeps its PID. | `test_git_is_not_launched_where_its_exit_status_would_be_discarded`; `test_a_git_no_longer_running_when_its_caller_is_cancelled_is_sent_no_signal` (reaped by someone else; reaped by the cleanup itself), which cancels after the reaping and asserts zero signals; `test_an_exit_status_someone_else_collected_is_not_a_success` keeps the `ECHILD` case covered now that the `SIGCHLD` route is refused earlier. |
| 2B-6.2 a close error escapes | The whole capture lifetime is inside one boundary: any `OSError` not already turned into `NotStarted` before the launch is `RemoteUnavailable`. Cancellation is not caught. | `test_output_files_that_cannot_be_closed_are_an_unknown_outcome`, and the two-project test you asked for: `test_a_project_whose_git_output_cannot_be_closed_does_not_hold_up_another` (close fails for the first project's fetch; its intent stays outstanding with zero sends; the second settles in the same pass). |

What I know is left: `signal.getsignal` only sees a disposition Python knows about. If native code set `SIGCHLD` to ignore behind Python's back, the precondition is not triggered; the `ECHILD` handling and the check before the signal still apply, with the narrow window between that check and the signal.

## Verification

`make check` passes: 397 tests, plus one unit test added afterwards (the "reaped by the cleanup itself" case), which passes with its file. Four guards were each broken once; three were caught at once and the fourth has that added test.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
