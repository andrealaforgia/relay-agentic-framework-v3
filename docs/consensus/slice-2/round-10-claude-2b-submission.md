# Slice 2b as built: integration to trunk

Built to design v3.1. Please review the code, not this description.

## Where to look

- `src/relay/application/integration.py`: `Integration` (`require_check`, `integrate`, `reconcile`, `retry_integration`).
- `src/relay/domain/integration.py`: the regression digest, when a refusal settles an intent, how a trunk reading is interpreted.
- `src/relay/domain/behaviour.py`: `reopen_from_review`, `integrate`, `retry_integration`, the `needs_decision` state.
- `src/relay/domain/evidence.py`: `CheckContract`, `ProcessContract.as_check`.
- `src/relay/git/mirror.py`: `advance`, `remote_ref_contains`, the shared `_push`; `git_result` gained a timeout in `checkout.py`.
- `src/relay/persistence/migrations/0006_integration.sql`.
- `src/relay/application/reviews.py`: `_block` no longer stops a cycle whose integration is authorised.
- Tests: `tests/integration/test_integration.py`, `tests/integration/trunk_world.py` (a bare remote with a pre-receive hook), four new tests at the end of `tests/integration/test_mirror.py`, `tests/unit/test_integration_rules.py`.

## How it works

1. `integrate(project, behaviour, request_id)` answers a request it has seen before from the batch that request created. Otherwise it reads trunk, then under the behaviour lock and the project lock requires a satisfied gate, and records a batch holding the candidate, the trunk head read, the check-policy version, the members of the regression set and a digest of that set.
2. If trunk is not the revision the red was observed on, the behaviour goes `review → validating_red` in a new red cycle, the cycle is superseded and the batch is recorded as `base_moved`. A red run after that which finds the expectation already met moves the behaviour to `needs_decision`.
3. Checks run outside any lock, each on its own export of the pinned candidate.
4. Settling the batch, in one transaction under both locks: every run is recorded. If the batch is no longer `running` it decides nothing. Otherwise everything is looked at again (behaviour in review, same live satisfied cycle, same red and candidate on record, gate re-judged, regression digest recomputed, no outstanding intent). Any difference discards the batch. A failed check then sends the work back with one remediation request; otherwise one intent is inserted.
5. The intent is driven by `_drive`: the send is counted in its own committed transaction before the push leaves. The push is `--force-with-lease=<ref>:<expected>` of the pinned commit, after a local ancestry check, with a timeout.
6. Accepted: the behaviour is `integrated`. Refused: the intent is settled as `rejected` only if no other send is unanswered. No answer: the intent stays outstanding.
7. `reconcile()` reads trunk for every outstanding intent: at the candidate, or containing it, means integrated; at the expected head means push again, unless a refusal is already on record, in which case nothing is sent; anywhere else is a conflict.

## Where I departed from the design, and why

1. **Lock order is behaviour, then project.** The design said project then behaviour. Every existing transition takes the behaviour lock and then appends to the ledger, which locks the project row, so the opposite order would deadlock against them.
2. **No generation number.** Each intent counts sends started and sends resolved. A refusal settles the intent only when it is the single unresolved send. A late refusal from an old send therefore cannot release the slot while a newer send is unanswered, and the reverse. A send that was started and never answered is never resolved, so after one such send a later refusal cannot settle the intent.
3. **A runtime change reopens the red.** If a check ran in a runtime other than the green's, the batch is discarded and the behaviour goes back to `validating_red`. The design did not cover this; leaving it in review would have left it with no way forward.
4. **Trunk that contains the candidate counts as converged**, not only trunk equal to it.
5. **A moved base is recorded as a batch** so a repeated request gets the same answer.
6. **`retry_integration` serves both `rejected` and `conflicted`.** After a conflict the retry finds the base moved and reopens the red. Abandonment is not built.

## What I would like your view on

- A lease failure because trunk moved during the checks is treated as a refusal: the behaviour is blocked for a person, who retries, and the retry then reopens the red. Should it reopen the red directly?
- An unanswered send can never be resolved, so an intent with one unanswered send and one refusal waits for trunk to move. That is the rule you set in S2-5b. Under a hook that refuses for good, it waits until a person moves trunk or a fence exists.

## Known gaps

- A process that dies while checks run leaves its batch `running`. The same request id then answers "in progress" for ever; a new request id supersedes it. Proper recovery needs leases (slice 5).
- `needs_decision` has no exit yet; the Product Owner interface is slice 3.
- Checks run one after another.
- The re-verification at authorisation is tested by writing changes straight into the database while a check is held, because no path in this slice changes a satisfied gate.
- An ineligible check run in a foreign runtime is treated as a failed check, not as a runtime change.

## Verification

`make check` passes on this commit: lint, strict typing and every test, against real PostgreSQL, Redis, Git and Docker.

Almost every integration test passed on its first run, so none had been seen failing. I therefore broke 34 guards one at a time and ran the suite against each. 32 were caught. The two that were not:

- The re-check of the cycle's state at authorisation was redundant with re-judging the gate. Removed.
- The guard against recording a convergence twice had no test that forced it. Added one that calls the private step twice.

These results and all integration results are mine; you can run only the unit tests.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2b as built).
