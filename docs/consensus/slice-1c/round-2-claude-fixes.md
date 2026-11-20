# Slice 1c, round 2: fixes

`make check`: 178 tests pass. Each guard below was broken deliberately and a test failed.

| Finding | Disposition | Fix | Test |
|---|---|---|---|
| C1 | ACCEPT | The fetched object must be a commit, and after checkout `HEAD` must equal the recorded candidate. | `test_an_attempt_ref_pointing_at_a_tag_never_reaches_review` (annotated tags pointing at the red commit and at a passing commit; no execution recorded) |
| C2 | ACCEPT | The subject runs as a fixed user, 65534:65534, on every host; the checkout is made world-readable so that user can read it. The runtime identity is now the resolved image id plus a digest of every container setting, so any change to those settings requires a fresh red. | `test_the_subject_runs_as_the_same_fixed_user_whoever_runs_the_supervisor`, `test_different_container_settings_give_a_different_runtime_identity` |
| C3 | ACCEPT | Attempt refs are `refs/relay/attempts/<project>/<behaviour>/<attempt>`. Project and behaviour ids are validated as identifiers. | `test_another_projects_candidate_on_the_same_repository_is_not_this_projects` |
| C4 | ACCEPT | `reopen_red(project, behaviour, reason)` returns an `implementing` behaviour to `validating_red`, drops the old pairing and records the reason. | `test_after_a_runtime_change_the_red_can_be_reopened_and_the_work_completed` (through to a new green), `test_reopening_a_red_needs_a_reason` |
| C5 | ACCEPT | The identity is resolved right after creation, so a timed-out run reports the same identity as a completed one. `judge_green` checks ineligibility before runtime equality. | `test_a_timed_out_run_still_reports_the_runtime_it_ran_in`, `test_a_candidate_that_hangs_is_reported_as_a_timeout` (application ledger, after a real accepted red) |
| C6 | ACCEPT | A behaviour id is validated when proposed, a project id when opened. | `test_a_behaviour_id_relay_could_never_build_is_refused_when_proposed` (only `project.opened` on the ledger) |
| Unlaunched rejections | ACCEPT | A direct refusal replaces the fictitious all-zero execution. | `test_native_execution_is_refused_for_green_before_anything_is_launched`, `test_with_no_red_on_record_green_is_refused_before_anything_is_launched` (zero launches, execution count unchanged) |
| Contract protection | ACCEPT | `docs/ARCHITECTURE.md` now says the contract record cannot be altered by a changeset, that the Verifier must name the public entry point, and that protection of repository-kept wrappers and fixtures is not built. | Documentation only |
| Regression and dispatch | ACCEPT | Both acceptance tests you asked for are written into slice 2's row in the slice table. Scenario 5 stays marked partial. | Documentation only |

Not done, stated: the world-readable checkout is not tested against a host where it would matter (Docker Desktop on macOS maps ownership loosely); attempt refs are still movable by a Builder between runs.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as now built).
