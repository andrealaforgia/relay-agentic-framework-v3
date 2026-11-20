# Slice 2a, round 4: fixes

`make check`: 288 tests pass. Each guard below was broken deliberately and a test failed.

The three findings share one cause: a Git helper that answered "no" when it had not found out. I fixed that at the root instead of case by case. `git_succeeds`, which turned every failure into `False`, is gone. `git_answers_yes` returns true on exit 0, false on exit 1, and raises on any other exit. Every caller was checked against the exit codes Git actually uses: `cat-file -e <hash>` and `rev-parse --verify --quiet` and `merge-base --is-ancestor` exit 1 for a real no; `cat-file -e "<hash>^{commit}"` exits 128 for an absent object, so presence and type are now asked separately. Tidying up a scratch ref uses a separate call that tolerates failure.

| Finding | Fix | Test |
|---|---|---|
| 2A-3.2 | The candidate ref is added to the recovery search only if it currently resolves on the remote. | `test_a_pending_candidate_that_is_truly_gone_is_blocked_once_not_left_pending`: the cycle is kept, the mirror removed, the attempt moved to unrelated history, reconciliation run twice; one block with the lost-commit reason, no review commands. |
| 2A-3.3 | A fatal error in the ancestry or presence check now raises and the cycle is deferred. | `test_an_ancestry_check_that_fails_defers_instead_of_declaring_the_commit_lost`, `tests/unit/test_git_answers.py` |
| 2A-4.4 | Push status is classified by its bracketed prefix: `[remote rejected]` is a refusal; `[rejected]` is the lease finding the ref present; anything else, `[remote failure]` included, is `RemoteUnavailable`. After any publish attempt the ref is read again: absent means deferred, another commit means conflict. | `test_a_push_of_unknown_outcome_that_left_no_ref_is_retried_not_blocked` (that exact status line, then a successful retry with one command per reviewer), `test_a_push_reported_as_already_there_while_the_ref_is_absent_is_retried_not_blocked` |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2a as now built).
