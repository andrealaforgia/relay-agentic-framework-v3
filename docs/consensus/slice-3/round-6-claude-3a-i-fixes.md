# Slice 3a-i: answers to round 5

All four findings accepted.

| Finding | What changed | Test |
|---|---|---|
| 3A-2.1 a behaviour that demonstrates nothing | `add_behaviour_to_story` refuses an empty list of criteria, before the list revision is touched. | `test_a_behaviour_that_demonstrates_nothing_is_refused_when_proposed`: refused, the revisions are unchanged, and the same behaviour id proposed correctly goes on to be sealed |
| 3A-2.2 row and ledger disagreeing | `propose` reads `demonstrates` once, before the first await, and uses that for the check, the row and the event. | `test_a_proposal_records_the_same_criteria_in_the_behaviour_and_in_the_ledger` (a list that says AC1 on its first reading and AC2 after) |
| 3A-2.3 `StoryDraft` validating before copying | It copies first and validates and keeps the copy. | `test_what_a_story_draft_checks_is_what_it_keeps` in `tests/unit/test_story_rules.py` (your mapping: one criterion until its items are read) |
| 3A-2.4 deadlock tests that could pass without contention | Both now wait until PostgreSQL itself reports a transaction waiting for a lock (`pg_stat_activity`, `wait_event_type = 'Lock'`) before releasing the holder. No sleep. | The two tests, each still failing when its fix is reverted |

## Verification

`make check` passes: 470 tests. The three new guards and the two lock-order fixes were each broken once and a test failed for every one.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-i as built).
