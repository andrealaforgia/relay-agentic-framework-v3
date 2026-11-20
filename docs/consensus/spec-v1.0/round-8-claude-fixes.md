# Round 8: the last blocker, and the three smaller points

`make check`: 116 tests, passing on repeated full runs. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| R5.2 (blocking) | Any address containing `://` is parsed; a malformed one is refused, as is one with a password, a query, a fragment, or any userinfo on http(s). The refusal never repeats the address. Local paths and scp-style addresses pass. | `test_a_repository_address_that_may_carry_a_credential_is_refused_and_stored_nowhere` (seven cases including a query token and two malformed authorities; project table and ledger stay empty; the secret is absent from the exception), `test_an_ordinary_repository_address_is_accepted` |
| R15 | Done now rather than in 1c: oracle containers use the `json-file` driver with one file capped at twice the capture limit. A rotated log no longer matches the attached capture, and the attached capture flags the overflow. | `test_output_beyond_the_capture_limit_is_an_overflow_in_a_container_too` |
| R6.1 test | The fixture asserts creation succeeded and that the named container existed at the moment of cancellation; the final check fails if the engine cannot be queried. | `test_a_container_created_just_before_cancellation_is_still_removed` |
| R13 follow-up | `migrate` raises if a recorded migration is missing from the build. | `test_an_applied_migration_missing_from_this_build_is_an_error` |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slices 1a and 1b as now built).
