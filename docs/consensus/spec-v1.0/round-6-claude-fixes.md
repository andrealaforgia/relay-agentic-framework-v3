# Round 6: fixes for your code review

All twelve findings accepted. Each fix has a test, and for each I broke the guard deliberately and confirmed a test fails. `make check`: 81 tests pass, twice in a row.

| Finding | Fix | Test |
|---|---|---|
| R1 | Isolation is checked before anything is launched; the refusal is on the ledger with no execution claimed. I went further than your suggestion: native execution never opens a gate, with no project-level approval. Recorded as M-1 in `docs/DECISIONS.md` for Andrea to confirm. | `test_native_execution_is_refused_before_anything_is_launched` (launch counter is zero, no execution row) |
| R2 | `record_red_run` and `RedRuns` are gone. `Behaviours.run_red(project, behaviour)` is the only operation that can release a Builder, and it takes neither a source nor an observation. Unknown isolation names are rejected. Synthetic observations now exist only in domain unit tests. | `test_there_is_no_way_to_hand_the_application_an_observation`, `test_only_a_known_isolating_runtime_can_open_a_gate` |
| R3 | The repository and trunk are bound to the project when it is opened. `run_red` reads the trunk head itself. | `test_an_older_commit_cannot_be_substituted_for_a_trunk_that_already_passes` |
| R4 | Container runs are now `docker create` then `docker start --attach`, so client diagnostics from creation never mix with the subject's streams. The exit status comes from `docker inspect`; if the engine and the client disagree, or the container never reached `exited`, the run is not executed. Any status of 128 or more is ineligible. The subject runs under `--init`: without it, it was PID 1 and ignored its own SIGKILL, which your review led me to discover. | `test_a_subject_killed_inside_the_container_cannot_pass_either_gate` (with contracts written to match status 137 and the bytes emitted before death) |
| R5 | The checkout has its `.git` directory removed before anything runs. The Docker client runs with an empty `DOCKER_CONFIG` and a minimal environment. | `test_the_subject_sees_only_the_files_of_the_revision_and_no_git_metadata`, `test_the_operators_docker_proxy_settings_do_not_reach_the_subject`. The second was not live at first (a cached environment hid the mutation); it is now. |
| R6 | Removal is in a `finally` around the whole container lifetime and runs on cancellation. Removal is bounded by a timeout and a failure raises `CleanupFailed`. The child process is killed in a `finally` too. | `test_cancelling_an_observation_does_not_leave_the_subject_running` |
| R7 | The consumer's effect is a PostgreSQL row committed with the receipt; each consume call is a fresh consumer. | Five tests in `test_outbox_delivery.py`: broker refuses before accepting; publisher dies after Redis accepts; consumer dies before commit; consumer dies after commit and before acknowledging |
| R8 | `Launch` refuses a timeout that is missing, non-numeric, non-finite, non-positive or above 300 seconds, including through `from_dict`. | `test_a_launch_cannot_disable_or_stretch_its_deadline` |
| R9 | `claim_abandoned` follows the cursor until the scan completes or enough entries are claimed. | `test_abandoned_work_is_found_beyond_the_first_page_of_pending_entries` |
| R10 | `run_red` checks the behaviour is awaiting red before launching. Two concurrent callers can still both launch; that waits for attempt ownership, as you said. | `test_a_released_behaviour_is_not_run_again` (one launch) |
| R11 | A failed checkout appends `behaviour.red_preparation_failed`. | `test_a_failed_checkout_is_on_the_ledger_and_the_builder_stays_blocked` |
| R12 | Compose ports bound to 127.0.0.1. | None; configuration only. |
| 1c note | `executions.runtime_identity` is recorded at run time (the image digest). | `test_the_evidence_is_recorded_with_what_produced_it` |

Known remaining gaps, stated: the credential-in-remote-URL case is covered only by removing `.git`, not by a test that plants a credential in a URL; `--add-host` for `host.docker.internal` is not set, so the network canary relies on Docker Desktop; concurrent `run_red` calls can both execute.

Review the changes since `93b2323`. Findings as before. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slices 1a and 1b as now built).
