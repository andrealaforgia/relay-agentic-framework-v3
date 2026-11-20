# Slice 1c, round 3: fixes

`make check`: 202 tests pass, twice in a row. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| C4.1 (blocking) | Green: the red on record is re-read under the final row lock and must equal the red the run was launched to answer, execution id included; otherwise the verdict is `red_superseded` and the behaviour does not move. Red: each behaviour has a `red_cycle`, incremented by `reopen_red` and persisted; a red run records the cycle it started in and is rejected as `superseded` if the cycle has moved on. | `test_a_green_in_flight_when_its_red_is_superseded_does_not_reach_review` and `test_a_red_in_flight_when_the_red_is_reopened_cannot_fill_the_new_cycle`: the first run is held at a barrier after observing, the red is reopened and a new one accepted (or not), then the first run is released. |
| C2.1 (blocking) | The identity comes from the engine's final inspection, falling back to the one taken at creation. With neither, the run is not executed and its identity is the literal `unresolved`. The configured image reference is never used as an identity. | `test_the_identity_is_the_engines_even_when_the_first_inspection_fails` |
| C2.2 (blocking) | `normalise_permissions`: directories 0755; a file is 0755 if Git marked it executable, else 0644; symbolic links untouched. | `tests/unit/test_checkout_permissions.py`: the same tracked files checked out owner-only and ordinarily end with identical modes. |
| C2.3 | `--workdir` moved into the settings tuple. The digest covers the settings, the mount target and the actual capture limit. | `test_the_runtime_identity_covers_the_mount_point_and_the_capture_limit` |
| C6.1 | Identifiers containing `..` or ending in `.lock` are refused. | `tests/unit/test_identifiers.py` |
| C4.2 | The build request's idempotency key is derived from the accepted red's execution id. | `test_a_fresh_red_issues_a_build_request_with_a_new_idempotency_key` |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as now built).
