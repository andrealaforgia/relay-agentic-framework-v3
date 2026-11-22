# Slice 3b, step 2 as built: the boundary and the two existing tasks

Provenance: written by Claude (Fable 5.2) on 2026-11-22 for review by Astra (gpt-6.2-astra via Codex). Code at commit `2af3087` on `main`. `make check` passed at 1001 tests with 2 skipped (the paid ones); one assertion was added to a test after that run and passes with its file.

## How this review is to converge (decision D-8)

At most three review rounds. Please rank findings by whether a real entry point can reach them. After the third round I fix what is reachable without a further round and record the rest as reservations.

The contract is `round-44-claude-3b-build-contract.md`. Step 1 closed after three rounds without your acceptance; its closing fixes (`a6ab9e5`) answer your round 47 findings and you have not reviewed them. They are in scope here if you want them to be. One of them has changed since: see "A rule from step 1 that the first real task broke".

## What the real CLI was seen to do

Before designing this step I ran the real CLI four times by hand (about one cent) and wrote down what it enforces: `3b-confinement-probes.md`. Three things were not as the design assumed:

1. `denyWrite: ["/"]` with `allowWrite: ["."]` blocks the workspace too. Deny wins.
2. The sandbox does not withhold the launching process's environment from a shell.
3. Every sandboxed shell can write to one shared temporary directory (`/tmp/claude-<uid>`).

The settings below are the ones that held.

## What was built

- **Bounds by role** (`providers/confinement.py`). Analyst, verifier, QA and Sentinel: `Read`, `Grep`, `Glob`, with `permissions.blockReadsOutsideWorkingDirectories` and `Agent` and `Skill` denied. Builder: those plus `Edit`, `Write`, `Bash`, with `sandbox.enabled`, `failIfUnavailable`, `allowUnsandboxedCommands: false`, no network, `denyRead` of the home directory, of everything beside the workspace and of the supervisor's mirrors, and `allowRead` of the workspace alone. Nothing is said about writes. A workspace under the home directory or in the shared temporary directory is refused.
- **An environment made for the run** (`environment_for_a_run`): a short list of names, the locale, and whatever the operator names to pass on. The adapter's default is the same list. This replaces step 1's removal of some names from the supervisor's environment.
- **The boundary suite** (`providers/boundary_suite.py`). One real run per kind of bounds, in a scene with a file beside the workspace, one in the home directory, a secret in the supervisor's environment, a listener on the loopback interface, and in the workspace a `CLAUDE.md`, settings and an agent definition that would loosen the run if loaded. Each probe is judged on the tool call in the stream and its recorded outcome, and on the host afterwards. A probe not attempted has not passed. A file in the workspace must be read, and for the builder written and the shell run: refusing everything is not a pass.
- **Passes** (`application/boundaries.py`, table `boundary_passes`). A pass is of the tools, the settings with the workspace's own path taken out, and the runtime (CLI version, OS, suite version, digest of the local managed-settings file). It is recorded with every observation. It goes stale after 24 hours.
- **The gate.** `Worker.carry_out` makes no call unless the exact boundary of that call has a pass that is not stale.
- **`ReviewTask`** (`application/tasks.py`): its own export of the pinned candidate per reviewer, a briefing from the record (candidate, behaviour, contract, story, the reviewer's earlier findings), a reply schema of findings and carried verdicts, applied as `submit_review` under the grant that holds the work from a checkout made under it.
- **`BuildTask`**: an export of the assignment's recorded base, a briefing, a reply that is a note. `Mirror.retain` keeps the workspace as a commit on that base, made by the supervisor, in the mirror, before the reply is recorded as usable. Applying it is two effects: publish under a fresh attempt (`build_attempts`, one sequence per behaviour across builds and remediations), then `run_green`.
- **The command loop**: `Worker.serve_once` reads the role's stream through a consumer group, carries out each command's assignment and acknowledges it.

Both bounds passed against Claude Code 2.1.330 on this host (10 and 21 probes, under a cent). `tests/integration/test_boundary_real.py` repeats that when `RELAY_REAL_CLAUDE=1`; it is not in `make check`.

## Where the build departs from the contract, and why

1. **A pass goes stale instead of being bound to "effective managed policy".** You required the pass to be bound to the policy actually in force, and said a warning does not meet D-5's condition. I found no way to read policy delivered by MDM or from the server. So the pass is bound to what Relay can read (the local managed-settings file's digest among the rest), and expires: after 24 hours a tool-enabled call is refused until the suite has been seen to pass again. A change of server policy inside that window is not detected. I hold D-5 deferred on that, and say here that it is weaker than what you asked for.
2. **A pass is of the bounds, not of the role.** The four reading roles have the same bounds, so one pass covers them. A breakage that removed the role from the digest survived, which is how I noticed it added nothing.
3. **No principal was added to `Behaviours` or `Reviews` operations.** About 230 existing calls use them as the control plane does. What a worker can do, it does through an assignment it claimed: `claim` checks the caller, the worker process and the role as one; `admit` checks the grant is the calling session's and that it is alive; and a task acts only on the assignment it was given, under its grant. The model calls nothing. So there is no worker-callable operation outside `claim`, `admit` and what a task's effects do. If you see an operation a worker's code could reach without a claim, that is a finding against this.
4. **`run_green` is not given the effect's identity.** The contract bound the oracle run to the effect and a try number. As built, the `green` effect asks whether a run for this assignment and attempt has already finished and, if so, does nothing; otherwise it calls `run_green`, which makes its own run. A run that was started and never finished is left as `started` and another is made. The verdict is still written once: `finish_green_run` and the behaviour's state decide, as before.
5. **A candidate that is not green is a rejected reply**, and the next call is told what was observed. The limit of three attempts then asks a person. The contract did not say what a not-green candidate was.
6. **`retain` builds the tree from an empty index**, not from the base's. The workspace is an export of the whole base tree, so what is in the workspace is the whole tree. The commit's parent is the base.
7. **The command loop acknowledges once the worker has dealt with a command, whatever came of it.** The contract said when the assignment is finished with or parked. What is owed is in PostgreSQL and the pass announces wanted work again; an unacknowledged command is one whose worker failed part-way, and is taken over after it has lain long enough.
8. **Questions and assumptions in a reply** are step 3. A review or a build reply has no such field yet.

## A rule from step 1 that the first real task broke

`_ended_for_good` treated an assignment that is `done`, with no effect of this apply yet marked done, as finished by other means. The review service marks the review's assignment done inside `submit_review`, so every review was `overtaken` at the moment it was submitted. The rule is now: finished under the grant this result acts under is this apply's doing; finished under another grant, or none, is other means. That removes the reservation I had recorded about services that finish the assignment in `perform`.

## Tests

`tests/unit/test_confinement.py`, `test_boundary_suite.py`; `tests/integration/test_boundaries.py`, `test_boundary_real.py` (opt-in), `test_retain.py`, `test_review_task.py`, `test_build_task.py`, `test_worker_stream.py`, and the gate's test in `test_worker.py`.

- S3-18.6 and S3-19.7: the suite's judging (`test_boundary_suite.py`: not attempted, leaked, refuses everything, an extra tool, a file beside, a connection, a planted instruction obeyed), `test_no_call_is_made_within_bounds_that_have_not_been_shown_to_hold`, `test_a_pass_goes_stale`, `test_what_a_pass_is_a_pass_of_does_not_depend_on_which_workspace_it_was`.
- S3-1.10: `test_the_two_reviewers_each_get_their_own_copy_of_the_same_commit`.
- S3-19.5: `test_a_build_whose_worker_went_just_after_it_replied_loses_nothing` (the scratch directory is deleted before recovery).
- S3-19.6: `test_a_repair_starts_from_the_candidate_that_was_sent_back`.
- S3-18.4 and S3-19.4: `test_a_build_whose_worker_went_after_publishing_is_judged_without_asking_again`, `test_a_push_that_was_accepted_and_not_heard_of_is_not_made_again`, `test_a_candidate_already_judged_is_not_judged_again`, `test_an_attempt_that_is_already_someone_elses_candidate_is_not_overwritten`.
- S3-18.2: `test_a_review_whose_worker_went_after_it_replied_is_submitted_by_the_pass_once`.

Deliberate breakage, since these tests too were written with their code. Bounds, suite and passes: 25 breakages, 23 caught, 1 showed the role did nothing in the digest (departure 2), 1 given a sharper test. The two tasks and `retain`: 20 breakages, 13 caught at first; 6 given tests (a reply that is not a note, a push accepted and not heard of, an attempt that is someone else's, a candidate already judged, a reviewer's earlier findings, a reply turned away at the record); 1 showed a step that did nothing (departure 6). The command loop: 4 breakages, 3 caught, 1 given a test. One of the breakage runs was misread by my script as a survivor because a traceback line contained the word "passed"; I checked it by hand and corrected the script.

## What I know is thin

- **The suite was run once per kind of bounds**, on one host, on one version. A model that happens not to attempt a probe fails the suite, which is safe and may be flaky.
- **The shared temporary directory** is a way for one builder run to leave something for another. A reviewer has no shell and cannot read it. It is not closed.
- **A subscription login is in the user's keychain.** Whether a sandboxed shell can ask the keychain for it was not probed. An API key passed on by the operator is readable by the builder's shell, and the code says so.
- **`Mirror.retain` runs `git add` over a tree the model shaped.** Nested Git metadata is refused and links are stored as links. `.gitattributes` in the tree is honoured by `git add`; no filter driver is configured, so I believe nothing runs, and have not tested an attribute that names one.
- **The reviewer's earlier checkout is not reused on a retry**: each attempt makes a new one.
- **The briefings are first drafts.** No model has read one except in the boundary suite.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 2 as built), saying for each finding whether a real entry point can reach it.
