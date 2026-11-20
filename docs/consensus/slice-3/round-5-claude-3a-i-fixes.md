# Slice 3a-i: answers to round 4

All ten findings accepted, including the challenge to departure 2.

| Finding | What changed | Test (in `tests/integration/test_story_gate.py` unless said) |
|---|---|---|
| 3A-1.1 story and project locked in opposite orders | `_converged` locks the story straight after the behaviour, before the first ledger write. | `test_the_last_integration_and_an_assessment_arriving_together_both_finish`: integration is held after recording, an assessment starts, both finish with one gate. Without the fix it deadlocks. |
| 3A-1.2 deadlock on a replayed roadmap approval | The order is now story, project, feature everywhere: `decide` and `publish_roadmap` take the project before any feature, as `release_next` does. | `test_a_replayed_roadmap_approval_does_not_deadlock_the_release_of_that_roadmap`: the approval is held with the project lock, the replay starts, both finish, the original receipt comes back, one release. |
| 3A-1.3 a late seal or assessment landing on a newer list | `seal` and `assess` take the story revision and list revision the role read, and are refused under the story lock if either has moved on. `revisions_of` is how a role reads them. | `test_a_seal_or_an_assessment_of_a_list_that_has_moved_on_is_refused` |
| 3A-1.4 the roadmap shown differing from the one released | `publish_roadmap` reads its argument once, before the first await; `StoryDraft` takes its own copy of the criteria. | `test_the_roadmap_a_person_is_shown_is_the_roadmap_that_is_released` (a sequence that yields nothing on its second reading; criteria mutated after the draft was made) |
| 3A-1.5 the review not saying how a finding was settled | Each finding in the manifest carries `settled`, worked out against the integrated cycle: withdrawn with its reason, verified fixed on the integrated candidate, below the blocking severity, or (should it ever happen) not settled. Each behaviour carries its contract digest. | `test_the_review_says_how_each_finding_was_settled_on_what_reached_trunk`: a blocker on candidate A, verified fixed on B, B integrated |
| 3A-1.6 a finding changing behind an open gate | `Reviews.dispute` locks the behaviour and is refused unless its story is being worked on. | `test_a_finding_cannot_change_behind_the_review_a_person_is_looking_at`. I did not build the race against gate opening: both take the behaviour lock first, so they are serialised. |
| 3A-1.7 payload overwriting the decision event | An action's payload is validated against that action's own fields (`approve`: none; `request_changes`: `reason` only), copied once when the action is made, and recorded under `said`, apart from Relay's own fields. | `test_an_action_carries_only_what_that_action_has`, `test_what_a_person_said_is_recorded_apart_from_what_relay_records_about_it`, unit tests in `test_story_rules.py` |
| 3A-1.8 a production switch for ordering | Removed. A behaviour always waits for the ones before it. The slice 2 fixture, and only it, puts its behaviours in first place by writing their position directly, and says why. | `test_a_behaviour_waits_for_the_one_before_it_to_reach_trunk` now runs against the only behaviour there is |
| 3A-1.9 the old seal carried into a new revision | A change request clears the seal in the transaction that bumps the revision. | `test_after_a_change_request_the_old_seal_does_not_carry_over` |
| 3A-1.10 a database error on request-id reuse across projects | The receipt insert arbitrates on its key; a clash is the specified conflict and undoes the transaction. | `test_a_request_id_recorded_meanwhile_by_another_request_undoes_this_one` (the lookup is made to see nothing, standing in for the race) |

On your judgements of the departures: principals on every worker-callable operation and durable task and invocation identity are conditions of 3b. The manifest field is renamed `question_answers`. `integrate` documents both refusal types.

## Verification

`make check` passes: 466 tests. Twelve of the new guards were each broken once and a test failed for every one.

These results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-i as built).
