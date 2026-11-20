# Slice 3a-ii, step 1 as built: pause, resume and cancel

Provenance: written by Claude (Fable 5.2) on 2026-11-20 for review by Astra (gpt-6.2-astra via Codex). Code at commit `1a3e1ab` on `main`. `make check` passes: 531 tests against real PostgreSQL, Redis, Git and Docker, and the same gate now runs in GitHub Actions on every push.

## How this review is to converge (decision D-8)

The Product Owner has asked that reviews converge. This step gets **at most three review rounds**. Please rank findings by whether they are reachable through Relay's real entry points (`Behaviours`, `Reviews`, `Integration`, `Stories`, `Assignments`, `Features`, `OutboxPublisher`). After the third round I fix what is reachable without a further round and record the rest as named reservations in the README. A finding that needs a fault nobody has shown can occur should be marked as such.

## What was built against design v3 (round 10) and its section 6a

- **Gate lock and closure** (`application/closure.py`, migration `0008`): one row per project, shared by every workflow transaction, exclusive for pause, resume and cancel. `features.closed_by`.
- **Assignments** (`application/assignments.py`, migration `0009`): `build`, `remediate`, `review`, `integrate`; states wanted, granted, done, retired. `Assignments.claim(principal, assignment_id)` returns a grant; the same worker gets the same grant; a paused or cancelled feature grants nothing.
- **First effects name the grant**: `Behaviours.run_green(project, behaviour, attempt, grant)` commits a `green_runs` record before launching and settles against that one assignment; `Reviews.prepare_review(capability, into, grant)` records the checkout receipt as the grant's first effect.
- **Pause** revokes grants with no first effect; **cancel** retires every assignment of the feature, frees the slot and releases the next story in the same transaction; a reopened red retires the behaviour's assignments.
- **Recording under closure**: a red finishing after a cancel is superseded; a green whose assignment is no longer current is `assignment_superseded`, opens no cycle; a review submitted after a cancel is stored stale; checks settling under pause are `held`, under cancel `abandoned`.
- **`Features`** (`application/features.py`): `close` (the entry point for pause, resume, cancel), `reconcile(project)` and `start()`. The pass: on start revoke every grant and mark unfinished green runs interrupted; close held batches (and, on start, running ones) as interrupted, held or abandoned by the feature's state; settle outstanding intents; publish pending candidates; announce wanted work again; take up owed integrations.
- **Successors**: `Integration.continue_integration(assignment_id)` starts generation n+1 of the same request in the transaction that takes the assignment up, only if the parent is the latest batch of the behaviour's live cycle; otherwise the assignment is retired with `integration.successor_stale`.
- **Publisher**: commands of a closed feature are not selected; `announce_again` re-arms the same message id.

## Where the build departs from the agreed design, and why

1. **Owing a remediation does not retire an earlier build.** Deliberate breakage showed the retirement could never fire: a remediation becomes owed only after a cycle exists, and a cycle exists only after an accepted green has completed the build. A partial unique index refuses a second live build or remediation per behaviour.
2. **No counts of announcements requested and published.** The publisher holds its outbox row lock from selection to mark-sent, and `announce_again` updates unconditionally, so it waits for an overlapping publish and its request stands afterwards. `test_announcing_again_while_the_first_announcement_is_being_sent_is_not_lost` shows it, and the conditional variant of the update fails that test.
3. **The pass does not evaluate story gates or release the next story.** The resume transaction itself does both, under the exclusive gate lock; at start nothing can be owed that the event's own transaction did not already do.
4. **No `publish_candidate` assignment.** `review_cycles.publication = 'pending'` is already the durable record that publication is owed, and the pass scans it.
5. **The control plane is the integrator in this slice.** The pass takes up `integrate` assignments itself, so a resume runs the regression checks before it returns. The assignment goes from wanted to done in the transaction that creates the successor batch, with its grant recorded there. A separate consumer arrives in 3b.
6. **A stale check on the successor is one condition, not four**: the parent must be the latest batch of the behaviour's live cycle. A replaced cycle, an integrated cycle and newer checks all fail it. The three separate conditions (behaviour in review, same cycle, cycle satisfied) could not be made to matter by any reachable history and were removed.
7. **A resume that changed nothing still runs the pass.** It is idempotent, and a repeated resume after a crash between the transaction and the pass must be able to finish the job.

## Tests

`tests/integration/test_closure.py` (22), `test_assignments.py` (22), `test_reconciliation.py` (18), plus rules in `tests/unit`. The four exit histories:

1. Red in flight: `test_a_red_that_finishes_during_a_pause_is_judged_and_its_build_waits`, `test_a_red_that_finishes_after_a_cancel_is_kept_as_evidence_only`, `test_work_announced_before_a_restart_is_announced_again_after_it`.
2. Review in flight: `test_a_review_checked_out_before_a_pause_may_finish_during_it`, `test_a_review_granted_before_a_pause_is_granted_afresh_after_it_and_done_once`, `test_a_review_of_a_cancelled_feature_is_kept_and_decides_nothing`, `test_a_review_checked_out_by_a_process_that_died_is_granted_afresh_and_done_once`.
3. Last regression check in flight: `test_checks_that_finish_after_a_closure_authorise_nothing`, `test_checks_held_by_a_pause_run_again_after_the_resume_and_integrate_once`, `test_checks_left_running_by_a_process_that_died_are_closed_when_the_next_one_starts` (open, paused, cancelled), `test_an_integration_owed_for_checks_that_were_since_replaced_starts_nothing`.
4. Intent already authorised: `test_a_push_authorised_before_a_pause_still_lands_and_opens_no_gate_*`, `test_a_push_authorised_before_a_closure_is_settled_by_the_next_start` (pause, cancel).

Deliberate breakage across the step: 84 mutants. 72 were caught at once; 6 guards were shown redundant and removed; 6 led to new tests. None survives.

## Known limits I would record as reservations

- Tests stand a recording object where Redis would be for the publisher cases; delivery over real Redis Streams is covered by `test_outbox_delivery.py` and `test_review_cycle.py`, not repeated here.
- "Restart" in tests is new service objects over the same database, with the old task cancelled and its tidy-up suppressed. No test kills an operating-system process.

Please review the code at `1a3e1ab` against design v3 and section 6a, and against the departures above.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 as built), and for each finding say whether it is reachable through a real entry point.
