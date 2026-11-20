# Slice 3a-ii, step 1 as built: closing the step after round 3

Provenance: written by Claude (Fable 5.2) on 2026-11-20. Not sent to Astra: under D-8 the third round was the last, and these fixes close the step without a further review. That they are unreviewed is recorded as a reservation in the README.

All three findings of round 14 accepted and fixed. Each fix was broken on purpose afterwards and its test failed.

| Finding | Fix | Test |
|---|---|---|
| 3AII-3.1 a policy change during a resume leaves the work waiting | The pass looks again for owed integrations after taking some up, up to three rounds, never trying the same assignment twice in one pass. A repository that could not be read is therefore not retried within the pass. | `test_checks_taken_up_by_relay_and_overtaken_by_a_new_required_check_are_run_again` (one resume, one policy change, one integration, nothing else called); `test_a_pass_does_not_chase_checks_that_keep_being_overtaken_for_ever` |
| 3AII-3.2 one project's silent repository holds up the others | `Reviews(pool, mirrors, remote_timeout=120.0)`: every repository call on the publication and recovery path (ref reads, the listing of attempts, the recovery fetch, the candidate push) is bounded. An unanswered one leaves publication pending and recorded as deferred. | `test_one_projects_silent_repository_does_not_keep_the_pass_from_another_project` |
| 3AII-3.3 the reason for a re-run is dropped | The interrupted batch and its ledger event keep the reason the checks could no longer decide. | asserted in the first test above |

Limits of these fixes, stated plainly:

- The silent-repository test stalls the ref read. The listing of attempts, the recovery fetch and the push are given the same timeout by the same code, but no test stalls each of them.
- Three rounds of taking up is a number I chose. Checks overtaken more often than that in one pass wait for the next resume or start.
