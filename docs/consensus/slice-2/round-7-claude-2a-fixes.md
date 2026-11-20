# Slice 2a, round 3: fixes

`make check`: 282 tests pass. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| 2A-3.1 | `Mirror.recover` has three outcomes. Pinned: true. Every ref fetched and none contains the commit: false. Any fetch that could not be completed, with the commit still not found: `RemoteUnavailable`. Publication then defers. | `test_a_fetch_cut_short_does_not_declare_an_available_commit_lost` (pin missing, listing succeeds, fetch fails, then connectivity returns and the original commit is published), `test_recovery_that_could_not_finish_looking_does_not_say_the_commit_is_gone` |
| 2A-4.1 | `refs_under` raises `RemoteUnavailable` like `resolve_ref`. Independently, the publication loop catches any Git failure from one cycle, defers that cycle and continues. | `test_a_repository_that_cannot_be_listed_is_unavailable`, `test_one_projects_unreachable_repository_does_not_hold_up_another` (two projects in one pass; the first has no pin and its repository is gone; it is deferred and the second is published), `test_an_unforeseen_git_failure_defers_the_cycle_instead_of_stopping_the_pass` |
| 2A-4.2 | No more matching on words in stderr. `Mirror.publish` runs `git push --porcelain` and reads the status line for the destination ref: `[remote rejected]` raises `RemoteRejected` with the remote's summary; any other per-ref rejection (the lease found the ref present) raises plain `CheckoutFailed`; no status line for the ref raises `RemoteUnavailable`. | `test_a_push_to_a_repository_that_is_not_there_is_unavailable_not_refused`, `test_a_hook_that_declines_is_a_refusal_by_the_remote_with_its_reason`, `test_an_existing_ref_is_neither_a_refusal_nor_an_outage`, `test_a_push_that_never_reached_the_remote_leaves_the_cycle_pending` |
| A consequence I found | With real push statuses, the loser of a race between two publishers is told `[remote rejected]` (it could not lock the ref). It now looks at the ref again and blocks only if the candidate is still not published. | `test_two_publishers_at_once_announce_each_task_once`, run three times |
| 2A-1.1 | The assertion on physical row order is removed. | Unchanged otherwise |
| 2A-4.3 | Agreed: the guarantee is one event each time the reason changes. | Unchanged |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2a as now built).
