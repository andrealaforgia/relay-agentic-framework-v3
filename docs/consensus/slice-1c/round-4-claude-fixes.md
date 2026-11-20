# Slice 1c, round 4: fixes

`make check`: 210 tests pass. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| C4.1a (blocking) | The lifecycle check stays at launch. At completion nothing raises: under the lock, a run whose behaviour has left the state it was launched in, whose cycle has moved on, or whose red is no longer the one on record is judged `superseded` (red) or `red_superseded` (green). The execution is stored and the rejection event references it; state and pairing are untouched. This also covers two runs in the same cycle, where the second to finish finds the first already accepted. | `test_a_green_released_after_reopening_but_before_the_new_red_is_still_recorded`, `test_a_red_released_after_the_replacement_red_was_accepted_is_still_recorded` (your two orderings, with the rejection event's execution id looked up in `executions`), plus three domain tests |
| C2.2a | Permissions are now set by the checkout itself, from `git ls-files --stage` read before the metadata is removed: tracked mode 100755 gives 0755, 100644 gives 0644, directories 0755. The oracle no longer touches permissions. | `test_permissions_in_a_checkout_come_from_git_not_from_the_host` (umask 022 and 077), `test_an_executable_whose_bit_the_filesystem_dropped_is_still_executable` |
| C2.3a | Two tests that vary one thing each, on the identity function directly: the mount target alone (settings asserted identical), and two capture limits below the log-size floor. | `test_the_runtime_identity_changes_with_the_mount_point_alone`, `test_the_runtime_identity_changes_with_the_capture_limit_alone` |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as now built).
