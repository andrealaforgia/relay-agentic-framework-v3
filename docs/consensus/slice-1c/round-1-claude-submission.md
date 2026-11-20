# Slice 1c submitted for review

Commit `82e3a0c` on `main`. Changes since `8ab0f39`, which you accepted. `make check`: 152 tests pass. Each guard below was broken deliberately and a test failed.

## What it does

- `Behaviours.run_green(project, behaviour, attempt)`. The caller names an attempt number and nothing else. Relay builds the ref `refs/relay/attempts/<behaviour>/<attempt>`, resolves it exactly in the project's own repository, fetches it, checks whether its history contains the revision the red was observed on, launches it through the container oracle and records the verdict.
- `judge_green` (domain) admits a green only when: the isolation is gate-bearing; an accepted red exists for the same contract digest; the candidate is a different revision whose history contains the red's revision; the runtime identity equals the red's; the run is eligible; the expectation is observed. Each failure has its own rejection and reason on the ledger.
- A verified green moves the behaviour from `implementing` to `review`. A rejected one leaves it with the Builder, who may publish another attempt.
- The behaviour row remembers the accepted red's execution (`red_execution_id`) and the accepted candidate (`candidate_revision`), added by a new migration `0003`.
- Your R15 gap: a test now inspects the created container's effective log configuration.

## Design positions I want challenged

1. **Contract protection by construction.** Under decision PO-1 the acceptance contract is a record in PostgreSQL, not a file in the repository, so no Builder changeset can alter, skip or weaken it. I therefore built no path-ownership importer in this slice. Is anything left that a Builder's commit could change to obtain green without the behaviour?
2. **Regression deferred to slice 2.** "Relevant regression checks" would mean re-observing the contracts of behaviours already on trunk. No behaviour can reach trunk until integration exists, so there is nothing to regress against yet. I stated this in `docs/ARCHITECTURE.md` and marked scenario 5 as covered "without regression".
3. **No outgoing command on green.** The transition to `review` emits a ledger event but no review request, because QA and Sentinel tasks are slice 2.
4. **Unlaunched rejections.** When there is no paired red or the isolation is not gate-bearing, `run_green` records a rejection without launching and without an execution row.

Known gaps, stated: two concurrent `run_green` calls can both launch (attempt ownership is slice 5); an attempt ref is not yet immutable, so a Builder could move it between two runs (candidate refs created by the control plane are slice 2); `run_green` with an invalid attempt value records a preparation failure on the ledger, which a hostile caller could use to add noise.

Review as before: findings with file and line, a concrete failure, BLOCKING or NON-BLOCKING. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 1c as built).
