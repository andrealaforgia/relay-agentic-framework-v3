# Slice 1c, round 5: fixes

`make check`: 215 tests pass. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| C2.2b (blocking) | Before any permission is changed, every tracked regular file must be found as a regular file (`lstat`, never following a link), reached through no symbolic link in any directory component, and distinct from every other tracked file by device and inode. Otherwise the checkout fails with `CheckoutFailed`, which the application records as a preparation failure before the oracle is reached. Only then are modes applied, and only to those verified paths. | `test_a_tracked_file_that_turned_into_a_link_fails_the_checkout_and_touches_nothing_outside`, `test_a_tracked_file_reached_through_a_linked_directory_fails_the_checkout` (both assert the external file keeps mode 0600), `test_two_tracked_names_that_are_one_file_on_this_host_fail_the_checkout`, `test_a_tracked_link_is_left_as_a_link_and_its_target_untouched`. The collision is injected between checkout and normalisation, so the tests do not depend on the host filesystem being case-insensitive. Zero oracle launches on a failed checkout is already covered by `test_a_failed_checkout_is_on_the_ledger_and_the_builder_stays_blocked`. |
| C2.2c | The listing is kept as bytes through path handling. | `test_a_file_name_that_is_not_valid_text_is_checked_out_or_refused_explicitly` |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as now built).
