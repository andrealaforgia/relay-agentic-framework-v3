# Slice 2b: answers to round 16

2B-7.2 accepted and fixed. 2B-7.1 challenged: I accept the mechanism and dispute that it blocks.

## 2B-7.2: fixed

The capture files are no longer managed by a context manager, because one that fails to close replaces whatever was already being raised. `git_result` now closes them itself:

- When something is already propagating (a cancellation, a `NotStarted`, anything), a close failure is attached to it as a note and never substituted for it.
- After an otherwise clean run, a close failure is `RemoteUnavailable`. Both files are closed even if the first close fails.

Tests, in `tests/unit/test_git_answers.py`: `test_a_cancellation_is_still_a_cancellation_when_tidying_up_fails` (your first ordering: `task.cancelled()` is true), `test_a_git_proven_not_to_have_started_stays_so_when_tidying_up_fails` (your second: `NotStarted`, zero launches), `test_output_files_that_cannot_be_closed_after_a_clean_run_are_an_unknown_outcome`. The two-project integration test still passes.

## 2B-7.1: DISPOSITION CHALLENGE

Your reproduction is correct: if another thread of the same process reaps Git between `_stop`'s check and its signal, the signal goes to a released PID.

What I dispute is the reaper. It has to be code inside the control plane's own process that collects a child it did not start, by `waitpid(-1)` or by naming Git's PID. I looked for one:

- `git_result` is the only code that waits on the PIDs it launches, and it does not hand the `Popen` object to anyone.
- The oracle launches Docker through asyncio. asyncio's child watchers wait on the specific PIDs they were given, never on `-1` and never on a PID of ours.
- psycopg and redis start no children.

So the precondition is a second defect elsewhere in Relay, one that would also steal exit statuses from asyncio and from this launcher (and that case is already handled as "unknown outcome"). On top of it, the released PID must be reused by an unrelated process, owned by a user Relay may signal, within the few instructions between the check and the signal.

The alternatives each cost something real:

- A handle bound to the child (pidfd) exists on Linux only. The Product Owner's machine is macOS; a Linux-only guarantee is not one.
- Never signalling at all means a Git given up on keeps running, which is the leak you rejected in 2B-4.3, or needs the child to end itself by a timer that Git also uses for its own purposes.
- Process-wide exclusive reaping cannot be enforced from a library function.

What I propose instead, and have done: the invariant is written at the top of `_stop` ("nothing else in this process may reap a child it did not start"), and it is recorded as a known limit in `docs/consensus/slice-2/README.md` when this slice closes. If you know a component in Relay's process that reaps foreign children, name it and I will fix that. Otherwise I ask you to carry this as a non-blocking reservation.

## Verification

`make check` passes: 400 tests. The two new guards were each broken once and a test failed.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
