# Slice 2a, round 5: fixes

`make check`: 290 tests pass. Each guard below was broken deliberately and a test failed.

| Finding | Fix | Test |
|---|---|---|
| 2A-3.4 | A mirror is "initialised" only if it has a `HEAD`. `holds` answers false for a directory that exists without one, and recovery then initialises it. | `test_a_mirror_directory_left_empty_by_an_interrupted_start_does_not_stop_recovery`: pending cycle, mirror directory present and empty, attempt intact; one pass publishes and announces both reviews. |
| 2A-3.5 | Adoption catches any Git failure for one behaviour, records `review.adoption_deferred` with the reason (once while it stays the latest thing said about that behaviour) and moves on. | `test_one_unreadable_legacy_mirror_does_not_stop_adoption_for_another_project`: two legacy reviews in one pass, an ancestry error injected for the first; the second is adopted; one deferral event across two passes; after restoring, the first is adopted exactly once. |

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2a as now built).
