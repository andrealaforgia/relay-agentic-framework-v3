# Slice 1c, round 6: fixes

`make check`: 218 tests pass. Each guard below was broken deliberately and a test failed.

**C2.2b.1 (blocking).** I replaced the per-case checks with one complete one. After the metadata is removed and before any permission is changed, the checkout is scanned without following any link, and the set of entries on disk (exact byte names, each as file, link or submodule placeholder) must equal the set of tracked entries from `git ls-files --stage`. Any difference fails the checkout: two directories folded into one, two files folded into one, a tracked file that is now a link, a tracked link that is now a file, a file reached through a linked directory, or a file that is not tracked at all. Permissions are then applied only to the verified regular files.

Tests: `test_two_tracked_directories_folded_into_one_fail_the_checkout` (your `rates/standard` and `Rates/reduced` case; the commit is built with index plumbing, and the fold is forced on hosts that would keep them apart, so the outcome is the same on any filesystem), `test_two_tracked_names_folded_into_one_file_fail_the_checkout`, `test_a_file_that_is_not_tracked_fails_the_checkout`, `test_a_tracked_link_that_became_a_file_fails_the_checkout`, and the two earlier link tests, which still assert that a file outside the checkout keeps its mode. A failed checkout raises before the oracle is reached.

**C2.2c test gap.** On a successful checkout the test now reads the file under its original byte name and checks its content.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as now built).
