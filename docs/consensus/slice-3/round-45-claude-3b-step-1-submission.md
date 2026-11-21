# Slice 3b, step 1 as built: records and the adapter

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). Code at commit `e114f19` on `main`. `make check` passed at 888 tests; two tests were added after that run and pass with their files (890 in all). The suite has not been run in one piece since those two additions.

## How this review is to converge (decision D-8)

At most three review rounds. Please rank findings by whether a real entry point can reach them. After the third round I fix what is reachable without a further round and record the rest as reservations.

The contract is `round-44-claude-3b-build-contract.md`: design v2 as amended by v3, with your round 43 findings folded in. You have not reviewed that folding; if it misreads a finding, say so.

No task is driven yet. Step 1's apply is given by the caller, as a list of effects. The tests drive a real `build` assignment (claimed after a real verified red) through the records with effects of their own.

## What was built

- **The stream reader** (`providers/stream.py`): the start event, tool calls and their outcomes, and the terminal event kept whole and classified by its own fields. Usage is by the model billed; a counter not reported is `None`.
- **The price table** (`providers/pricing.py`): dated 2026-11-03; cache reads priced per model; cache writes by lifetime; no estimate where a model is unknown, a counter is missing, writes come without their lifetimes, or a model priced by prompt size is over its threshold in total. Its estimates agree with what two real calls were billed.
- **Agent definitions** (`providers/definitions.py`): configured paths, then the project, then the home directory; path, hash, repository revision and whether the file differs from it.
- **The adapter** (`providers/claude.py`): four endings (`returned`, `errored`, `not_started`, `uncertain`); briefing on standard input; an environment without the supervisor's own settings; its own process group, killed with it, also when the caller is cancelled; the definition passed inline with the role's tools; flags checked against the installed CLI before any call. A run whose start event lists a tool or a server it was not given is ended.
- **Workers and liveness** (`application/workers.py`, migration `0015`): a session-level advisory lock held on a connection of the worker's own. `is_alive` asks PostgreSQL.
- **The grant's owner** (`Assignments.claim(principal, assignment, worker)`): recorded on the grant; a worker that is not running is refused; parked work is refused.
- **Invocations** (`application/invocations.py`): `admit`, `record`, `apply`, `decide`, `recover`, `parked`.
- **The runner** (`application/worker.py`): `Worker.carry_out(assignment)` claims, then loops: prepare, admit, call, record, apply; a call that gave nothing usable is followed by another only if admission allows it, briefed with what was wrong.
- **The pass** (`Features.reconcile`) calls `Invocations.recover` when it is given one. `revoke_every_grant` now takes back only grants with no worker recorded.

Three small real calls were made to the cheapest model (under a cent in all) to record the streams the tests replay: a success, a budget exhaustion, and the success cut off before its terminal event.

## Where the build departs from the contract, and why

1. **There is no "retry consumed" marker.** The contract had admission consume a person's permission. As built, a retry unparks the work and announces it; the next admission admits because the work is no longer parked and the last call gave nothing. A late result is decided by whether a later call of the same assignment exists, under the assignment's lock. A replayed `retry` is answered from its receipt; a new `retry` naming a call that is no longer the latest is stale. A breakage of the marker survived, which is how I found it did nothing.
2. **Admission does not look at `parked` or at an uncertain last call.** Both were built and removed for the same reason: parking takes the grant back, so nobody can present a current grant for parked work. `claim` refuses parked work. The test for that is the claim's.
3. **"Ended for good" is "the assignment was retired".** A cancel, a rejection and a reopened red all retire the assignment, so a separate test for a cancelled feature never fired. The comparison of revisions and decision basis with what the call was admitted on is kept, before the first effect only.
4. **The grant is not compared when an effect is taken.** The contract had it. A late result that wins takes its original grant back if nobody has claimed the work since; if somebody has, the result is applied under an assignment that is granted to another worker. I dropped the comparison instead of refusing that case, because the services the effects call check grants themselves. This is the departure I am least sure of.
5. **Every apply is a list of effects**, including those the contract called "one transaction". A one-effect apply whose effect is idempotent under its identity is the same thing, and it is one mechanism to recover instead of two. It does mean a service called by an effect must be idempotent under the effect's identity, which step 2 has to make true for `submit_review`.
6. **Retention of a build's tree is a parameter of `record`** (`retained`), stored with the row. Step 1 has no builder, so nothing passes it yet.
7. **The inbox item for a parked call is not a gate.** `inbox_items` has a kind `invocation` and names the call; `Invocations.parked` lists them and `Invocations.decide` decides them. `Stories.inbox` still lists gates only. One view over both is 3c's.
8. **A grant with no worker recorded** is still possible: `claim` takes the worker as an optional argument, because about four hundred existing tests claim without one. Such a grant is treated as belonging to the control plane's own process and is taken back when it starts, as before. Step 2, which puts a principal on every worker-callable operation, is where I intend to make it required.
9. **The lock order test for `reopen_red` is sequential.** It shows a reopened build admits no call. It does not race the two.

## Tests

`tests/unit/test_claude_stream.py`, `test_pricing.py`, `test_agent_definitions.py`, `test_claude_adapter.py`; `tests/integration/test_invocations.py`, `test_worker.py`. By finding:

- S3-18.1: `test_a_delivery_that_comes_again_never_buys_a_second_call`, `test_the_same_work_delivered_again_calls_nothing`.
- S3-18.2 and S3-19.3: `test_a_live_workers_call_is_left_alone_whoever_else_starts`, `test_a_worker_that_is_gone_before_it_called_anything_leaves_the_work_wanted`, `test_the_pass_applies_what_a_worker_that_is_gone_had_already_been_told`, `test_a_worker_must_be_running_to_be_granted_work`.
- S3-18.3: `test_a_pause_before_the_call_is_admitted_stops_it_and_one_after_does_not_lose_it`, `test_a_pause_between_a_reply_that_could_not_be_used_and_the_next_call_stops_it`.
- S3-18.4 and S3-19.4: `test_an_apply_cut_short_is_carried_on_from_the_effect_it_had_reached`, `test_two_deliveries_of_one_result_do_not_both_carry_out_an_effect`, `test_a_cancel_between_two_effects_stops_the_second`.
- S3-18.5: the adapter and stream tests; `test_an_effort_the_cli_cannot_be_asked_for_is_refused_and_not_dropped`.
- S3-18.10: `test_what_a_call_returned_is_stored_whole_with_what_it_used`, `test_an_estimate_is_of_the_whole_call_or_it_is_not_given`, `test_pricing.py`.
- S3-19.2: `test_a_result_that_arrives_late_and_first_is_used_and_the_retry_is_not`, `test_a_result_that_arrives_after_its_retry_was_admitted_is_kept_and_not_used`, `test_a_result_that_arrives_before_anyone_decided_ends_the_waiting`, and the replay inside `test_a_worker_gone_during_its_call_leaves_a_person_to_say_whether_to_call_again`.
- S3-20.1: that last test (two workers), `test_the_attempts_are_counted_over_the_work_whoever_was_granted_it`.
- S3-20.2: `test_a_try_that_was_taken_over_while_it_ran_decides_nothing`.
- S3-20.3: `test_a_known_result_is_held_through_a_pause_and_applied_once_after_it`, `test_a_result_about_a_story_that_has_since_changed_is_not_applied`, `test_a_known_result_for_a_build_that_was_reopened_is_never_applied`.
- S3-20.4: `test_a_build_that_was_reopened_admits_no_call` (sequential, as above).
- S3-20.5 (the part that is every run's): `test_a_run_that_has_a_tool_or_a_server_it_was_not_given_is_ended`.

All of these tests were written with the code and passed the first time they ran, so none was seen to fail before its code existed. Deliberate breakage is what stands in for that. Provider modules: 24 breakages, 18 caught, 6 given tests. Invocation records: 47 breakages, 32 caught; of the rest, 4 showed code that did nothing, which was removed (departures 1 to 3), and the others were given tests. The runner: 10 breakages, 8 caught, 2 given tests. Every survivor was run again and is now caught.

## What I know is thin

- **Recovery of a call whose worker lost its connection but is still running** is tested by stopping the worker's session, not by cutting a connection under a live process.
- **`is_alive` asked from a pooled connection** takes and releases the dead worker's lock. Two passes asking at once may each see the other's momentary hold as life, and leave the work for the next pass. That errs toward waiting.
- **The stub CLI** proves how Relay calls the CLI and reads what it prints. It proves nothing about what the real CLI enforces; that is step 2's suite.
- **No time-based lease** (slice 5): a worker that hangs with its connection open holds its grant.
- **The runner has no loop over a stream of commands yet**: `carry_out` is one piece of work. Reading the role's stream and acknowledging is step 2.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 1 as built), saying for each finding whether a real entry point can reach it.
