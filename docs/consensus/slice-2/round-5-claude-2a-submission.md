# Slice 2a submitted for review

Built to design v3.1. `make check`: 265 tests pass. Each guard named below was broken deliberately and a test failed.

## What exists

- `src/relay/git/mirror.py`: one bare repository per project on the control plane's disk. `pin_from` fetches one named ref into a scratch ref, requires it to be the expected commit and a commit at all, and pins it under `refs/relay/pins/<commit>`. Exports and publication take the commit from the pin. Publication names one destination and requires it to be absent. Replacement objects are disabled for all trusted Git. A green run pins its candidate before the oracle sees it and answers ancestry from the mirror.
- `src/relay/domain/review.py`: the gate as a pure function over the cycle's frozen policy, the roles that have submitted, the behaviour's findings and the findings verified as fixed on this candidate.
- `src/relay/application/reviews.py` and migration `0005`: review cycles (identity independent of the commit; candidate ref named by the cycle id), tasks, capabilities, prepared checkouts, submissions, findings, per-cycle verifications, remediation requests.
  - A verified green creates the cycle and tasks in its own transaction; no command exists yet.
  - `publish_candidates` publishes, then in one transaction marks the cycle published, issues capabilities and enqueues the commands.
  - `prepare_review` confirms the published ref still names the cycle's commit, exports the tree from the pin and records a receipt; otherwise it blocks the cycle.
  - `submit_review` answers in the order of S2-2a.
  - `adopt_reviews_without_a_cycle` and `announce_unreviewed_tasks` are the recovery passes.
- `tests/integration/test_review_cycle.py` (21 tests), `tests/integration/test_mirror.py`, `tests/unit/test_review_gate.py`.

## Things you should know before reading

1. **A defect my own tests found.** Two control-plane processes publishing the same candidate at once: the loser's push was refused and it blocked the cycle. Now a refused push is followed by a second look at the ref, and only a ref that does not name the judged commit blocks.
2. **Capabilities are stored in plaintext** in `review_tasks` and in the outbox row, so that a waiting task can be announced again with the same command. Anyone who can read the database can act as any reviewer. I judged that acceptable because the database is already the authority for everything; say so if you disagree.
3. **Stale arrivals are rare by construction.** A cycle only closes when every required reviewer has submitted, so a task with no effective submission in a closed cycle arises only when the cycle was blocked. The test for it uses the moved-ref case.
4. **`blocked` has no exit.** A cycle blocked by a ref conflict, a lost commit or a parked finding stays blocked. No recovery command exists yet.
5. **Not built, as designed:** authenticated reviewer identity, the Product Owner decision that settles a parked finding, and everything in 2b.

Review as before. End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 2a as built).
